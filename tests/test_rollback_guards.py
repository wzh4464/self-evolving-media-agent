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
    ("{trash_subdir}", "隔离区里的一个目录（死种按 content_path 整季搬进来的 B1 形态）"),
])
def test_restore_from_trash_rejects_bad_sources(lib, operator_cwd, tmp_path, trash_path, why):
    outside = tmp_path / "elsewhere" / "victim.mkv"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not in trash")
    subdir = lib.cfg.trash_dir / "2026-09-08" / "尼古喵喵" / "Season 1"
    subdir.mkdir(parents=True)
    (subdir / "尼古喵喵 S01E11.mkv").write_bytes(b"a whole season")
    src = trash_path.format(cwd=operator_cwd, outside=outside,
                            trash_dir_itself=lib.cfg.trash_dir, trash_subdir=subdir)
    dst = lib.show("测试番").season(1).path / "测试番 S01E01.mkv"
    _write_audit(lib, "r1", {"op": "restore_from_trash", "path": str(dst),
                             "trash_path": src, "torrent_record_lost": False})
    before_cwd = _tree(operator_cwd)

    res = lib.rollback("r1")

    assert res["skipped"] == 1 and res["reverted"] == 0, why
    assert not dst.exists(), why
    assert outside.read_bytes() == b"not in trash"
    assert (subdir / "尼古喵喵 S01E11.mkv").read_bytes() == b"a whole season"
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


def test_restore_from_trash_never_overwrites_an_occupied_destination(lib):
    """原位置此后又有了文件（换源重下的同一集）：`shutil.move` 在 POSIX 上会直接覆盖它。"""
    day = lib.cfg.trash_dir / "2026-09-20" / "测试番" / "Season 1"
    day.mkdir(parents=True)
    src = day / "测试番 S01E03.mkv"
    src.write_bytes(b"trashed")
    dst = lib.show("测试番").season(1).local("测试番 S01E03.mkv", size=1000)
    ident = lib.ident(dst)
    _write_audit(lib, "r6", {"op": "restore_from_trash", "path": str(dst),
                             "trash_path": str(src), "torrent_record_lost": False})

    res = lib.rollback("r6")

    assert res["skipped"] == 1 and res["reverted"] == 0
    assert "占用" in res["skipped_detail"][0]["skip_reason"]
    assert lib.ident(dst) == ident and src.read_bytes() == b"trashed"


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
    # —— 以下每条单独守住一道闸：删掉对应的检查，这条就会变成 reverted / failed / 改动了东西
    ({"op": "relink_torrent", "torrent_hash": "a" * 40,
      "mapping": [{"old": "a.mkv", "new": "../../escaped.mkv"}]}, "relink 映射越出种子根"),
    ({"op": "relink_torrent", "torrent_hash": "a" * 40,
      "mapping": [{"old": "/etc/passwd", "new": "a.mkv"}]}, "relink 映射是绝对路径"),
    ({"op": "restore_title_aliases", "bangumi_id": None, "prev": None}, "别名还原缺 bangumi_id"),
    ({"op": "restore_title_aliases", "bangumi_id": 7, "prev": None},
     "别名还原但 AB 库不可用（基座默认没有 abdb）"),
    ({"op": "restore_rss_link", "bangumi_id": 0, "prev_rss_link": "x"}, "RSS 还原缺 bangumi_id"),
    ({"op": "ungrab_episode", "show_dir": "{show}", "season": 1, "episode": "x"},
     "ungrab 集号不是整数"),
    ({"op": "rename_show_dir", "path": "{show}", "new_name": "旧名",
      "torrent_savepaths": [["a" * 40, "relative/Season 1"]]}, "目录改名的 save_path 是相对路径"),
    ({"op": "rename_show_dir", "path": "{show}", "new_name": "旧名",
      "prev_savepath": "relative"}, "目录改名的 AB save_path 是相对路径"),
    ({"op": "readd_torrent", "magnet": "magnet:?xt=urn:btih:" + "b" * 40,
      "save_path": "relative"}, "readd 相对 save_path"),
    ({"op": "remove_tags", "torrent_hash": "a" * 40, "tags": ""}, "remove_tags 缺 tags"),
    ({"op": "rename", "path": "{show}/Season 1/../Season 1/a.mkv", "new_name": "b.mkv",
      "torrent_hash": ""}, "路径含 .. 未规范化"),
    ({"op": "restore_sidecar", "show_dir": "{media}", "prev": "{{}}"},
     "sidecar 目录就是媒体库根本身"),
])
def test_every_undo_handler_validates_its_inputs(lib, operator_cwd, undo, why):
    show = lib.show("测试番")
    a = show.season(1).local("a.mkv", size=1000)
    (operator_cwd / "a.mkv").write_bytes(b"cwd file")
    undo = {k: (v.format(show=show.path, media=lib.media_root) if isinstance(v, str) else v)
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


@pytest.mark.parametrize("undo", [
    {"op": "restore_title_aliases", "bangumi_id": None, "prev": "[]"},
    {"op": "restore_rss_link", "bangumi_id": 0, "prev_rss_link": "https://x.invalid/rss"},
])
def test_ab_undo_without_bangumi_id_is_refused_even_when_the_ab_db_is_there(lib, undo):
    """有 AB 库时，缺 bangumi_id 的还原以前会执行 `UPDATE … WHERE id=NULL`——
    停一次容器、改零行、再报"已还原"。"""
    lib.bangumi(id=7, official_title="测试番", title_raw="Test Show")
    calls_before = lib.docker_log.read_text() if lib.docker_log.exists() else ""
    _write_audit(lib, "r8", undo, op=undo["op"])

    res = lib.rollback("r8")

    assert res["skipped"] == 1 and res["reverted"] == 0 and res["failed"] == 0
    assert "bangumi_id" in res["skipped_detail"][0]["skip_reason"]
    after = lib.docker_log.read_text() if lib.docker_log.exists() else ""
    assert after == calls_before                         # 没停过容器


@pytest.mark.parametrize("patch, why", [
    ({"priority": 0}, "还原成 0 不是还原"),
    ({"priority": "1"}, "优先级不是整数"),
    ({"priority": 8}, "超出 qBittorrent 的优先级范围"),
    ({"index": True}, "index 是布尔值（Python 里 True == 1，会命中第 1 个条目）"),
])
def test_restore_file_priority_validates_its_inputs(lib, patch, why):
    s1 = lib.show("测试番").season(1)
    pack = s1.torrent({"a.mkv": 1000, "b.mkv": 1000}, name="[G] Show 01-02",
                      layout="nosub", priorities={"b.mkv": 0})
    undo = {"op": "restore_file_priority", "torrent_hash": pack.hash, "index": 1,
            "name": "b.mkv", "priority": 1, **patch}
    _write_audit(lib, "r7", undo, op="trash")
    before = lib.qbit.snapshot()

    res = lib.rollback("r7")

    assert res["skipped"] == 1 and res["reverted"] == 0, why
    assert lib.qbit.snapshot() == before, why


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


# ------------------------------------------------------------------ 第 5 阶段的两种逆操作：只摘白名单里的、开关只认两个布尔
@pytest.mark.parametrize("entry", [
    {"field": "canonical_title", "value": "被伪造的标题"},                   # 不在白名单：回退不能借它摘别的
    {"field": "episode_offsets", "value": -24},                            # 按键摘的字段缺 key
    "episode_offsets",                                                     # 不是对象
])
def test_a_forged_unset_sidecar_record_is_refused(lib, entry):
    """`unset_sidecar` 只摘正向动作会补的那几项（`actions._UNSETTABLE`）：审计是历史数据、可能被手改。以前删掉白名单
    检查（审查的变异 T1-22），伪造的 `canonical_title` 条目在摘的时候 KeyError，不是干净的拒绝。"""
    sh = lib.show("回退甲")
    sh.sidecar(canonical_title="回退甲", episode_offsets={"3": -24})
    before = lib.sidecar("回退甲")
    _write_audit(lib, "forged-unset", {"op": "unset_sidecar", "show_dir": str(sh.path), "entries": [entry]},
                 op="adopt_episode_offset")

    res = lib.rollback("forged-unset")

    assert res["reverted"] == 0 and res["failed"] == 0 and res["skipped"] == 1
    assert "entries" in res["skipped_detail"][0]["skip_reason"]
    after = lib.sidecar("回退甲")
    assert (after.canonical_title, after.episode_offsets) == (before.canonical_title, before.episode_offsets)


ON = {"rss_parser.enable": True, "bangumi_manage.enable": True}


@pytest.mark.parametrize("undo", [
    {"op": "set_ab_mode", "flags": {"rss_parser.enable": "false", "bangumi_manage.enable": False},
     "set": ON, "prev_state": None},
    {"op": "set_ab_mode", "flags": {"rss_parser.enable": False}, "set": ON, "prev_state": None},
    {"op": "set_ab_mode", "flags": {"rss_parser.enable": False, "bangumi_manage.enable": False},
     "set": ON, "prev_state": {"mode": "half"}},
])
def test_a_forged_set_ab_mode_record_is_refused(lib, undo):
    """`set_ab_mode` 的逆操作只认两个开关各一个布尔、`prev_state` 是合法的模式记录：否则不 PATCH、不重启 AB。`set` 就是
    AB 此刻的开关（后面那道"有人改过"的检查拦不住它）。审查的变异 W1-20 把这道校验关掉，全套测试照样过——字符串
    "false" 会被当成真值发给 AB。"""
    _write_audit(lib, "forged-ab", undo, op="set_ab_mode")

    res = lib.rollback("forged-ab")

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert not [c for c in lib.ab.calls if c in ("update_config", "restart")]
