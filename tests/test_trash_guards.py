"""`_op_trash` 必须先确认要搬的是一个真实存在的普通文件，再碰 qBittorrent；
qBittorrent 那一步失败就停手，不能"种子没处理好，文件照搬"。

critic N1 的根因：`_op_trash` 先 `qbit.delete`（整种子作废），再看 `path.exists()`。
路径是幻影（种子声明了、磁盘上没有）或者在诊断与执行之间被挪走时，结果是：
种子记录丢了、文件一个没搬、审计写 `trashed_to: null` + 一条 `trash_path: ""`
的逆操作——正是 LAT-02 那 6 条定时炸弹的来源。生产实例：20260920T170126 删掉
朱音落语 S01E12 所属种子 d08f05a7 的记录，`freed 0`、`torrent_record_lost: true`。
"""
from __future__ import annotations

import shutil

import pytest

from media_agent.kernel import Action, Finding


def _trash(path, torrent_hash: str = "", *, file_only: bool = False, show: str = "朱音落语"):
    args = {"path": str(path), "torrent_hash": torrent_hash}
    if file_only:
        args["file_only"] = True
    return Finding(rule="duplicate-episode", kind="duplicate", severity="important",
                   summary=f"清理 {path}", show=show, path=str(path),
                   torrent_hash=torrent_hash, action=Action(op="trash", args=args))


def test_phantom_path_is_skipped_and_torrent_is_untouched(lib):
    """种子声明了文件，磁盘上却没有，而诊断**没有**把它标成幻影（`phantom`）——
    比如本轮诊断后才被挪走。盘上没东西可搬，也没有证据说该摘种子：跳过、一样不动。
    诊断时就确认是幻影的输家会被摘记录，见 `tests/test_phantom_slot.py`。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000, on_disk=False)
    before = lib.qbit.snapshot()

    rep = lib.apply([_trash(t.path, t.hash)])

    assert not rep.applied and not rep.failed
    [s] = rep.skipped
    assert "不在" in s["reason"]
    assert lib.qbit.snapshot() == before                      # 种子记录还在
    assert not [c for c in lib.qbit.calls if c[0] in ("delete", "set_file_priority")]
    assert all((r.get("undo") or {}).get("trash_path", "x") for r in lib.audit())


def test_file_moved_away_between_diagnose_and_apply(lib):
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000)
    f = _trash(t.path, t.hash)
    lib.fs_move("朱音落语/Season 1/朱音落语 S01E12.mp4", "朱音落语/S01E12.mp4")

    rep = lib.apply([f])

    assert [r["op"] for r in rep.skipped] == ["trash"]
    assert lib.qbit.has(t.hash)
    assert lib.trash_files() == []


@pytest.mark.allow("failed_record", match="拒绝")
def test_directory_is_refused_before_any_qbit_call(lib):
    """死种 content_path = Season 目录（NoSubfolder 多文件，生产 10 个）：绝不整目录搬。"""
    s1 = lib.show("尼古喵喵").season(1)
    other = s1.single("尼古喵喵 S01E01.mkv")
    t = s1.torrent({"尼古喵喵 S01E11.mkv": 600_000_000, "尼古喵喵 S01E11.ass": 50_000},
                   name="[TV版&无修版] 尼古喵喵 - EP11", layout="nosub")
    assert t.view()["content_path"] == str(s1.path)
    before = lib.snapshot()

    rep = lib.apply([_trash(s1.path, t.hash, show="尼古喵喵")])

    [fail] = rep.failed
    assert "目录" in fail["error"]
    assert lib.snapshot() == before
    assert lib.qbit.has(other.hash) and lib.trash_files() == []


@pytest.mark.allow("failed_record", match="媒体库")
def test_path_outside_media_root_is_refused(lib, tmp_path):
    outside = tmp_path / "downloads" / "x.mkv"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not ours")

    rep = lib.apply([_trash(outside)])

    assert len(rep.failed) == 1
    assert outside.read_bytes() == b"not ours" and lib.trash_files() == []


@pytest.mark.allow("failed_record", match="删除种子记录失败")
def test_qbit_delete_failure_leaves_everything_as_it_was(lib):
    """以前只记一行日志就继续搬文件：种子还在 qBit 里、文件却进了隔离区。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000)
    s1.local("朱音落语 S01E12 [BD].mp4", size=508_000_000)   # 集位里另有一份（删除关口 I1）
    lib.qbit.fail("delete", hash=t.hash)
    before = lib.snapshot()

    rep = lib.apply([_trash(t.path, t.hash)])

    [fail] = rep.failed
    assert "ReadTimeout" in fail["error"] or "timed out" in fail["error"]
    assert lib.snapshot() == before
    assert lib.trash_files() == []


@pytest.mark.allow("failed_record", match="设为不下载失败")
def test_set_file_priority_failure_leaves_everything_as_it_was(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.torrent({"尼古喵喵 S01E10.mkv": 600_000_000, "NCOP.mkv": 90_000_000},
                   name="[TV版&无修版] 尼古喵喵 - EP10", layout="nosub")
    lib.qbit.fail("set_file_priority", hash=t.hash)
    before = lib.snapshot()

    rep = lib.apply([_trash(s1.path / "NCOP.mkv", t.hash, file_only=True, show="尼古喵喵")])

    assert len(rep.failed) == 1
    assert lib.snapshot() == before                          # 优先级没变、文件还在原地


@pytest.mark.allow("failed_record", match="文件列表")
def test_files_failure_in_file_only_recheck_leaves_everything(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.torrent({"尼古喵喵 S01E10.mkv": 600_000_000, "NCOP.mkv": 90_000_000},
                   name="[TV版&无修版] 尼古喵喵 - EP10", layout="nosub")
    lib.qbit.fail("files", hash=t.hash)
    before = lib.snapshot()

    rep = lib.apply([_trash(s1.path / "NCOP.mkv", t.hash, file_only=True, show="尼古喵喵")])

    assert len(rep.failed) == 1
    assert lib.snapshot() == before


@pytest.mark.allow("failed_record", match="找不到")
def test_file_only_entry_not_in_torrent_is_refused(lib):
    """以前 `_torrent_rel_path` 返回 None 就静默跳过设优先级、照样搬文件：
    qBittorrent 仍把它当作要下载的文件，下一次校验就会重新下回来。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.torrent({"尼古喵喵 S01E10.mkv": 600_000_000, "NCOP.mkv": 90_000_000},
                   name="[TV版&无修版] 尼古喵喵 - EP10", layout="nosub")
    stray = s1.local("stray.mkv", size=1_000)
    before = lib.snapshot()

    rep = lib.apply([_trash(stray, t.hash, file_only=True, show="尼古喵喵")])

    assert len(rep.failed) == 1
    assert lib.snapshot() == before


def test_file_only_matches_the_entry_by_full_relative_path(lib):
    """合集里不同子目录下同名的 NCOP.mkv：只作废 `b/NCOP.mkv` 时，按文件名认条目会
    认成 `a/NCOP.mkv`——错的那个被设为不下载，要作废的这个却被搬进隔离区，
    qBittorrent 下次校验就把它重新下回来。必须按完整相对路径认。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"a/E05.mkv": 600_000_000, "a/NCOP.mkv": 90_000_000,
                       "b/E06.mkv": 600_000_000, "b/NCOP.mkv": 90_000_000},
                      name="[G] Yani Neko 05-06", layout="nosub")
    a_ncop, b_ncop = s1.path / "a" / "NCOP.mkv", s1.path / "b" / "NCOP.mkv"
    ident = lib.ident(b_ncop)

    rep = lib.apply([_trash(b_ncop, pack.hash, file_only=True, show="尼古喵喵")])

    assert len(rep.applied) == 1
    assert {f["name"]: f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]} == {
        "a/E05.mkv": 1, "a/NCOP.mkv": 1, "b/E06.mkv": 1, "b/NCOP.mkv": 0}
    assert a_ncop.exists() and not b_ncop.exists()
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident


@pytest.mark.allow("failed_record", match="torrent_record_lost|搬入隔离区失败")
def test_move_failure_after_torrent_delete_is_recorded_honestly(lib, monkeypatch):
    """搬文件失败时种子记录已经删了——failed 记录必须写明这一点，不能只剩一句异常。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000)
    s1.local("朱音落语 S01E12 [BD].mp4", size=508_000_000)   # 集位里另有一份（删除关口 I1）

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "move", boom)
    rep = lib.apply([_trash(t.path, t.hash)])

    [fail] = rep.failed
    assert fail["torrent_record_lost"] is True
    assert t.path.exists()


def test_normal_trash_still_moves_and_records_a_real_undo(lib):
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000)
    s1.local("朱音落语 S01E12 [BD].mp4", size=508_000_000)   # 集位里另有一份（删除关口 I1）
    ident = lib.ident(t.path)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.applied
    assert rec["undo"]["trash_path"] and rec["trashed_to"] == rec["undo"]["trash_path"]
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident and not lib.qbit.has(t.hash)
