"""回退（rollback）的逆操作必须先校验参数，再动任何东西。

critic N1 / LAT-02（2026-09-26 在 scratchpad 复现）：`restore_from_trash` 把空的
`trash_path` 变成 `Path('.')`——它是真值、而且"存在"——于是
`shutil.move('.', dst)`：`os.rename('.')` 报 EINVAL，shutil 退回
`copytree(当前目录 → 媒体库) + rmtree(当前目录)`。生产上有 6 条审计记录的
`trashed_to` 是 null（20260830T132317、20260908T022758 ×3、20260908T143348、
20260920T170126），离一次 `media-agent rollback --run …` 只差一步：
运维者的 cwd（项目目录：.venv、.env、state/ 里的审计与 14GB 隔离区）
会被拷进媒体库再整个删掉。

这里用合成的审计记录直接喂给回退，断言：什么都没搬、cwd 原封不动、
跳过原因写清楚。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_audit(lib, run_id: str, undo: dict, *, op: str = "trash") -> None:
    rec = {"ts": "2026-09-08T02:27:59", "run_id": run_id, "status": "applied",
           "dry_run": False, "rule": "dead-torrent", "kind": "dead_torrent", "op": op,
           "args": {}, "summary": "合成记录", "trashed_to": None, "freed_bytes": 0,
           "undo": undo}
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture
def operator_cwd(tmp_path, monkeypatch) -> Path:
    """模拟运维者在项目目录里敲 rollback：cwd 下有 .env、state/audit.jsonl 等。"""
    cwd = tmp_path / "operator_cwd"
    (cwd / "state").mkdir(parents=True)
    (cwd / ".env").write_text("QBIT_PASS=synthetic\n", encoding="utf-8")
    (cwd / "state" / "audit.jsonl").write_text("{}\n", encoding="utf-8")
    (cwd / "cli.py").write_text("# 假装是源码\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    return cwd


def _tree(p: Path) -> dict[str, bytes]:
    return {str(x.relative_to(p)): x.read_bytes()
            for x in sorted(p.rglob("*")) if x.is_file()}


def test_empty_trash_path_never_moves_the_cwd(lib, operator_cwd):
    """生产形态原样：dead-torrent 的 trash 记录，trash_path 为空、原位置已不存在。"""
    dst = lib.show("朱音落语").season(1).path / "朱音落语 S01E12.mp4"
    _write_audit(lib, "20260908T022758",
                 {"op": "restore_from_trash", "path": str(dst), "trash_path": "",
                  "torrent_record_lost": True})
    before_cwd = _tree(operator_cwd)
    before_lib = lib.disk()

    res = lib.rollback("20260908T022758")

    assert _tree(operator_cwd) == before_cwd          # cwd 一个字节都没动
    assert lib.disk() == before_lib                   # 媒体库里没多出任何东西
    assert not dst.exists()
    assert res["reverted"] == 0 and res["failed"] == 0 and res["skipped"] == 1
    [d] = res["skipped_detail"]
    assert "trash_path" in d["skip_reason"]


@pytest.mark.parametrize("trash_path, why", [
    (".", "相对路径"),
    ("state/trash/2026-09-08/x.mp4", "相对路径"),
    ("{cwd}", "cwd 本身（绝对路径，但是目录）"),
    ("{outside}", "隔离区之外的真文件"),
    ("{trash_dir_itself}", "隔离区根目录"),
])
def test_restore_from_trash_rejects_bad_sources(lib, operator_cwd, tmp_path, trash_path, why):
    outside = tmp_path / "elsewhere" / "victim.mkv"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not in trash")
    src = trash_path.format(cwd=operator_cwd, outside=outside,
                            trash_dir_itself=lib.cfg.trash_dir)
    dst = lib.show("测试番").season(1).path / "测试番 S01E01.mkv"
    _write_audit(lib, "r1", {"op": "restore_from_trash", "path": str(dst),
                             "trash_path": src, "torrent_record_lost": False})
    before_cwd = _tree(operator_cwd)

    res = lib.rollback("r1")

    assert res["skipped"] == 1 and res["reverted"] == 0, why
    assert not dst.exists(), why
    assert outside.read_bytes() == b"not in trash"
    assert _tree(operator_cwd) == before_cwd


@pytest.mark.parametrize("dst, why", [
    ("", "空目标"),
    ("Season 1/x.mkv", "相对目标"),
    ("{outside}", "媒体库之外"),
])
def test_restore_from_trash_rejects_bad_destinations(lib, operator_cwd, tmp_path, dst, why):
    day = lib.cfg.trash_dir / "2026-09-20" / "测试番" / "Season 1"
    day.mkdir(parents=True)
    src = day / "x.mkv"
    src.write_bytes(b"trashed")
    target = dst.format(outside=tmp_path / "elsewhere" / "x.mkv")
    _write_audit(lib, "r2", {"op": "restore_from_trash", "path": target,
                             "trash_path": str(src), "torrent_record_lost": False})

    res = lib.rollback("r2")

    assert res["skipped"] == 1 and res["reverted"] == 0, why
    assert src.read_bytes() == b"trashed"             # 隔离区里的文件原地不动
    assert not (tmp_path / "elsewhere").exists()
    assert not (operator_cwd / "Season 1").exists()


def test_restore_from_trash_still_works_for_a_sane_record(lib):
    """守卫不能把正常回退也拦掉。"""
    day = lib.cfg.trash_dir / "2026-09-20" / "测试番" / "Season 1"
    day.mkdir(parents=True)
    src = day / "测试番 S01E03.mkv"
    src.write_bytes(b"trashed")
    dst = lib.show("测试番").season(1).path / "测试番 S01E03.mkv"
    _write_audit(lib, "r3", {"op": "restore_from_trash", "path": str(dst),
                             "trash_path": str(src), "torrent_record_lost": False})

    res = lib.rollback("r3")

    assert res["reverted"] == 1 and res["skipped"] == 0
    assert dst.read_bytes() == b"trashed" and not src.exists()


@pytest.mark.parametrize("undo, why", [
    ({"op": "rename", "path": "", "new_name": "x.mkv", "torrent_hash": ""}, "rename 空路径"),
    ({"op": "rename", "path": "{show}/Season 1/a.mkv", "new_name": "../../x.mkv",
      "torrent_hash": ""}, "rename 目标名带路径"),
    ({"op": "rename", "path": "{show}/Season 1", "new_name": "Season 2",
      "torrent_hash": ""}, "rename 的对象是目录"),
    ({"op": "rename_show_dir", "path": "", "new_name": "旧名"}, "目录改名空路径"),
    ({"op": "rename_show_dir", "path": "{show}", "new_name": "a/b"}, "目录名带斜杠"),
    ({"op": "restore_sidecar", "show_dir": "", "prev": None}, "sidecar 空目录"),
    ({"op": "ungrab_episode", "show_dir": ".", "season": 1, "episode": 1}, "ungrab 相对目录"),
    ({"op": "relink_torrent", "torrent_hash": "", "mapping": []}, "relink 缺 hash"),
    ({"op": "relink_torrent", "torrent_hash": "a" * 40, "new_save_path": "relative",
      "mapping": [{"old": "a.mkv", "new": "b.mkv"}]}, "relink 相对 save_path"),
    ({"op": "readd_torrent", "magnet": "", "save_path": ""}, "readd 缺 magnet"),
    ({"op": "recategorize", "torrent_hash": "", "category": "x"}, "recategorize 缺 hash"),
    ({"op": "remove_tags", "torrent_hash": "", "tags": "x"}, "remove_tags 缺 hash"),
])
def test_every_undo_handler_validates_its_inputs(lib, operator_cwd, undo, why):
    show = lib.show("测试番")
    a = show.season(1).local("a.mkv", size=1000)
    (operator_cwd / "a.mkv").write_bytes(b"cwd file")
    undo = {k: (v.format(show=show.path) if isinstance(v, str) else v)
            for k, v in undo.items()}
    _write_audit(lib, "r4", undo, op=undo["op"])
    before_cwd = _tree(operator_cwd)
    before_lib = lib.disk(sidecars=True)
    before_qbit = lib.qbit.snapshot()

    res = lib.rollback("r4")

    assert res["skipped"] == 1 and res["reverted"] == 0 and res["failed"] == 0, why
    assert _tree(operator_cwd) == before_cwd, why
    assert lib.disk(sidecars=True) == before_lib, why
    assert lib.qbit.snapshot() == before_qbit, why
    assert a.exists()


def test_repair_ignores_records_with_bogus_paths(lib, operator_cwd):
    """`repair` 同样照着审计记录搬文件：空 path 会让它在 cwd 里 _merge_tree。"""
    (operator_cwd / "旧名").mkdir()
    (operator_cwd / "旧名" / "keep.txt").write_text("x", encoding="utf-8")
    _write_audit(lib, "r5", {"op": "rename_show_dir", "path": "", "new_name": "旧名"},
                 op="rename_show_dir")
    before_cwd = _tree(operator_cwd)

    from media_agent.actions import Executor
    res = Executor(lib.context(), dry_run=False, run_id="rp").repair_split_dirs("r5")

    assert res["pairs"] == 0
    assert _tree(operator_cwd) == before_cwd
