"""改动调用抛了异常：先按此刻状态核实它生效没有，再决定记什么（B2）。

qBittorrent 的 WebUI 超时（生产 2026-09-19/20 三次）常常发生在它**已经处理完**请求之后：
2026-09-14 run 20260914T100214，`Enticing.Circuit…mkv → 二十世纪电气目录 S01E11.mkv` 的 `renameFile`
读超时——qBittorrent 其实改了名，审计却记 failed、没有逆操作；此后那个集位一直"被占"、这次改名
也回退不了。

现在每个"能按此刻状态核实"的改动调用出错后都问一次：

- 生效了 → `applied`，带逆操作，`confirmed_after_error` 记下那个异常；
- 没生效（状态与动手前一致）→ `failed`，写明"按此刻状态核实：没有生效"；
- 读不到此刻状态、或状态与动手前 / 预期都对不上 → `unknown`，带"如果生效了该怎么撤"。

`FakeQbit.fail(..., after=True)` 模拟"改动生效之后响应丢了"；默认（`after=False`）是"请求没到"。
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from harness import video

from media_agent.actions import Executor
from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import UnrenamedDetector

SHOW = "二十世纪电气目录"
RAW_11 = "[Nekomoe kissaten][20 Seiki Denki Mokuroku][11][1080p][JPSC].mp4"
CANON_11 = f"{SHOW} S01E11.mp4"
PROBE = video("hevc", subs=["chi 简体中文"])


def _one_rename(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single(RAW_11, size=700_000_000, probe=PROBE)
    return s1, t


def _blind_after(lib, method: str, h: str):
    """`method` 照常生效，然后 qBittorrent 再也读不到（复核也读不了），再抛读超时。"""
    real = getattr(lib.qbit, method)

    def wrapped(*a, **k):
        real(*a, **k)
        lib.qbit.fail("files", hash=h, times=None)
        lib.qbit.fail("torrents", times=None)
        raise httpx.ReadTimeout("timed out (injected)")

    setattr(lib.qbit, method, wrapped)


def _unblind(lib, method: str):
    lib.qbit._faults.clear()
    delattr(lib.qbit, method)                  # 去掉实例上的替身，回到类方法


# ------------------------------------------------------------------ 改名（renameFile）
def test_rename_timeout_after_qbit_applied_it_is_applied_with_undo(lib):
    """生产 2026-09-14 原样：qBittorrent 改了名、响应超时。以前记 failed、没有逆操作。"""
    s1, t = _one_rename(lib)
    lib.qbit.fail("rename_file", hash=t.hash, after=True)

    c = lib.cycle(detectors=[UnrenamedDetector])

    [rec] = c.applied("rename")
    assert "ReadTimeout" in rec["confirmed_after_error"]
    assert rec["undo"] == {"op": "rename", "path": str(s1.path / CANON_11),
                           "new_name": RAW_11, "torrent_hash": t.hash}
    assert lib.qbit.file_names(t.hash) == [CANON_11]
    assert lib.rollback(c.run_id)["reverted"] == 1                  # 能回退了
    assert lib.qbit.file_names(t.hash) == [RAW_11]


@pytest.mark.allow("failed_record", match="ReadTimeout")
def test_rename_timeout_before_qbit_applied_it_is_failed(lib):
    """请求没到 qBittorrent：按此刻状态核实没有生效，才是 failed，且不带逆操作。"""
    _, t = _one_rename(lib)
    lib.qbit.fail("rename_file", hash=t.hash)

    c = lib.cycle(detectors=[UnrenamedDetector])

    [rec] = c.failed("rename")
    assert "没有生效" in rec["effect"] and "undo" not in rec
    assert lib.qbit.file_names(t.hash) == [RAW_11]


@pytest.mark.allow("unknown_record", match="renameFile")
def test_rename_timeout_with_qbit_unreadable_is_unknown_with_would_be_undo(lib):
    """超时之后连 qBittorrent 都读不到：说不清改没改。记 unknown，带上"如果改了该怎么撤"——
    回退时逆改名自己核对此刻状态（当前名不在就跳过）。"""
    _, t = _one_rename(lib)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    _blind_after(lib, "rename_file", t.hash)

    rep = Executor(lib.context(), dry_run=False, run_id="t-blind").apply(findings)

    [rec] = rep.unknown
    assert rec["op"] == "rename" and "ReadTimeout" in rec["error"]
    assert "复核" in rec["reason"]
    assert rec["undo"]["op"] == "rename" and rec["undo"]["new_name"] == RAW_11
    _unblind(lib, "rename_file")
    res = lib.rollback("t-blind")
    assert (res["reverted"], res["unconfirmed"], res["unconfirmed_reverted"]) == (1, 1, 1)
    assert lib.qbit.file_names(t.hash) == [RAW_11]


@pytest.mark.allow("unknown_record", match="renameFile")
def test_rename_whose_outcome_is_neither_before_nor_after_is_unknown(lib):
    """超时之后种子里既没有原名也没有目标名（别的进程同时改了它）：对不上就是不知道。"""
    _one_rename(lib)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    real = lib.qbit.rename_file

    def racing(h, old, new):
        real(h, old, "somebody-else.mp4")
        raise httpx.ReadTimeout("timed out (injected)")

    lib.qbit.rename_file = racing

    rep = Executor(lib.context(), dry_run=False, run_id="t-race").apply(findings)

    [rec] = rep.unknown
    assert "对不上" in rec["reason"]


def test_filesystem_rename_that_raised_after_moving_is_applied(lib, monkeypatch):
    """纯本地文件（没有种子）的 `Path.rename` 在搬完之后报错（网络卷上见过）：按盘上状态核实。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    loose = s1.local(RAW_11, size=700_000_000, probe=PROBE)
    real = Path.rename

    def rename(self, target):
        out = real(self, target)
        if Path(self) == loose:
            raise OSError(5, "Input/output error (injected)")
        return out

    monkeypatch.setattr(Path, "rename", rename)

    c = lib.cycle(detectors=[UnrenamedDetector])

    [rec] = c.applied("rename")
    assert rec["via"] == "filesystem" and "Input/output" in rec["confirmed_after_error"]
    assert (s1.path / CANON_11).exists() and not os.path.lexists(loose)


# ------------------------------------------------------------------ 标签 / 分类
def _tag(h: str) -> Finding:
    return Finding(rule="pin", kind="pin", severity="minor", summary="钉集号",
                   action=Action(op="retag", args={"torrent_hash": h, "tags": "ma:S01E11"}))


def _cat(h: str) -> Finding:
    return Finding(rule="category-consolidation", kind="category", severity="minor",
                   summary="交接分类", evidence={"current": "Bangumi"},
                   action=Action(op="recategorize", args={"torrent_hash": h, "category": SHOW}))


def test_retag_timeout_after_it_took_effect_is_applied(lib):
    _, t = _one_rename(lib)
    lib.qbit.fail("add_tags", hash=t.hash, after=True)

    rep = lib.apply([_tag(t.hash)], run_id="t-tag")

    [rec] = rep.applied
    assert rec["undo"] == {"op": "remove_tags", "torrent_hash": t.hash, "tags": "ma:S01E11"}
    assert "ReadTimeout" in rec["confirmed_after_error"]


@pytest.mark.allow("failed_record", match="ReadTimeout")
def test_retag_timeout_that_did_not_take_effect_is_failed(lib):
    _, t = _one_rename(lib)
    lib.qbit.fail("add_tags", hash=t.hash)

    rep = lib.apply([_tag(t.hash)])

    [rec] = rep.failed
    assert "没有生效" in rec["effect"] and "undo" not in rec


def test_recategorize_timeout_after_it_took_effect_is_applied(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single(RAW_11, size=700_000_000, probe=PROBE, category="Bangumi")
    lib.qbit.fail("set_category", hash=t.hash, after=True)

    rep = lib.apply([_cat(t.hash)])

    [rec] = rep.applied
    assert rec["undo"] == {"op": "recategorize", "torrent_hash": t.hash, "category": "Bangumi"}
    assert t.view()["category"] == SHOW


@pytest.mark.allow("unknown_record", match="setCategory")
def test_recategorize_timeout_with_qbit_unreadable_is_unknown_with_undo(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single(RAW_11, size=700_000_000, probe=PROBE, category="Bangumi")
    _blind_after(lib, "set_category", t.hash)

    rep = lib.apply([_cat(t.hash)], run_id="t-cat")

    [rec] = rep.unknown
    assert rec["undo"] == {"op": "recategorize", "torrent_hash": t.hash, "category": "Bangumi"}
    _unblind(lib, "set_category")
    assert lib.rollback("t-cat")["reverted"] == 1
    assert t.view()["category"] == "Bangumi"


# ------------------------------------------------------------------ 隔离 / 摘种子 / 设为不下载
def _trash(path, torrent_hash: str = "", *, file_only: bool = False, show: str = "朱音落语"):
    args = {"path": str(path), "torrent_hash": torrent_hash}
    if file_only:
        args["file_only"] = True
    return Finding(rule="duplicate-episode", kind="duplicate", severity="important",
                   summary=f"清理 {path}", show=show, path=str(path),
                   torrent_hash=torrent_hash, action=Action(op="trash", args=args))


def _akane(lib):
    """朱音落语 S01E12：要隔离的那份有种子，集位里另有一份（删除关口 I1 放行）。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=508_000_000)
    s1.local("朱音落语 S01E12 [BD].mp4", size=508_000_000)
    return s1, t


def test_trash_whose_torrent_delete_timed_out_after_removing_it_goes_on(lib):
    """以前：`delete` 超时就记「删除种子记录失败，文件未动」——而种子其实已经摘掉了，
    记录说它还在，文件也没进隔离区。现在核实种子确实不在了，照常把文件搬进隔离区。"""
    _, t = _akane(lib)
    ident = lib.ident(t.path)
    lib.qbit.fail("delete", hash=t.hash, after=True)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.applied
    assert "ReadTimeout" in rec["confirmed_after_error"]
    assert rec["undo"]["torrent_record_lost"] is True
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident and not lib.qbit.has(t.hash)


@pytest.mark.allow("unknown_record", match="删除种子记录")
def test_trash_whose_torrent_delete_cannot_be_confirmed_stops_unknown(lib):
    _, t = _akane(lib)
    _blind_after(lib, "delete", t.hash)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.unknown
    assert rec["path_still_at"] == str(t.path) and t.path.exists()   # 文件一个字节没动
    assert lib.trash_files() == []


def test_file_only_trash_whose_priority_change_timed_out_after_taking_effect(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.torrent({"尼古喵喵 S01E10.mkv": 600_000_000, "NCOP.mkv": 90_000_000},
                   name="[TV版&无修版] 尼古喵喵 - EP10", layout="nosub")
    lib.qbit.fail("set_file_priority", hash=t.hash, after=True)

    rep = lib.apply([_trash(s1.path / "NCOP.mkv", t.hash, file_only=True, show="尼古喵喵")])

    [rec] = rep.applied
    assert rec["undo"]["file_priority"]["index"] == 1
    assert "ReadTimeout" in rec["confirmed_after_error"]
    assert [f["priority"] for f in lib.qbit.raw(t.hash)["_files"]] == [1, 0]


def _move_then(monkeypatch, effect):
    """`shutil.move` 先按 `effect` 动一下盘，再抛 OSError（磁盘满之类）。"""
    import shutil

    real = shutil.move

    def move(src, dst, *a, **k):
        effect(real, Path(src), Path(dst))
        raise OSError(28, "No space left on device (injected)")

    monkeypatch.setattr(shutil, "move", move)


def test_move_that_raised_after_it_finished_is_applied(lib, monkeypatch):
    _, t = _akane(lib)
    ident = lib.ident(t.path)
    _move_then(monkeypatch, lambda real, src, dst: real(str(src), str(dst)))

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.applied
    assert "No space" in rec["confirmed_after_error"]
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident and rec["undo"]["trash_path"] == str(moved)


@pytest.mark.allow("failed_record", match="搬入隔离区失败")
def test_move_that_left_a_partial_copy_is_failed_and_the_copy_is_named(lib, monkeypatch):
    """跨卷搬运是先拷后删：拷到一半磁盘满，原文件还在库里、隔离区里多了半份。原文件没搬走，
    记 failed；那半份写进 `stray_copy`，隔离区处置认得出它、交给人（不再是"来历不明"）。"""
    from media_agent import purge as purge_mod

    _, t = _akane(lib)

    def half(real, src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"half")

    _move_then(monkeypatch, half)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.failed
    assert rec["torrent_record_lost"] is True and t.path.exists()
    [stray] = lib.trash_files()
    assert rec["stray_copy"] == str(stray)
    [c] = purge_mod.build_pool(lib.context())
    assert not c.eligible and "搬运失败" in c.why and c.origin == str(t.path)


@pytest.mark.allow("unknown_record", match="搬入隔离区")
def test_move_that_lost_the_source_and_left_a_short_copy_is_unknown(lib, monkeypatch):
    _, t = _akane(lib)

    def short(real, src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"short")
        src.unlink()

    _move_then(monkeypatch, short)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.unknown
    assert rec["undo"]["op"] == "restore_from_trash" and rec["trashed_to"] == rec["undo"]["trash_path"]
    assert rec["torrent_record_lost"] is True


def test_dead_drop_whose_delete_timed_out_after_removing_it_is_applied_with_readd(lib):
    from media_agent.plugins.builtin import DeadTorrentDetector

    s1 = lib.show("尼古喵喵").season(1)
    dead = s1.torrent({"尼古喵喵 S01E11.mkv": 600_000_000}, name="[A] Yani Neko - 11.mkv",
                      layout="single", progress=0.4, state="stalledDL",
                      added_hours_ago=24 * 30, availability=0, num_complete=0)
    lib.qbit.fail("delete", hash=dead.hash, after=True)

    c = lib.cycle(detectors=[DeadTorrentDetector])

    [drop] = c.applied("drop_torrent")
    assert drop["undo"]["op"] == "readd_torrent" and "ReadTimeout" in drop["confirmed_after_error"]
    assert not lib.qbit.has(dead.hash)


@pytest.mark.allow("unknown_record", match="删除种子记录")
def test_dead_drop_whose_delete_cannot_be_confirmed_is_unknown_with_readd(lib):
    from media_agent.plugins.builtin import DeadTorrentDetector

    s1 = lib.show("尼古喵喵").season(1)
    dead = s1.torrent({"尼古喵喵 S01E11.mkv": 600_000_000}, name="[A] Yani Neko - 11.mkv",
                      layout="single", progress=0.4, state="stalledDL",
                      added_hours_ago=24 * 30, availability=0, num_complete=0)
    findings = lib.diagnose(detectors=[DeadTorrentDetector])
    _blind_after(lib, "delete", dead.hash)

    rep = Executor(lib.context(), dry_run=False, run_id="t-drop").apply(
        [f for f in findings if f.action and f.action.op == "drop_torrent"])

    [rec] = rep.unknown
    assert rec["undo"]["op"] == "readd_torrent"


def test_skipping_a_downloading_extra_whose_priority_change_timed_out_after(lib):
    from media_agent.plugins.builtin import ExtrasDetector

    s1 = lib.show("银八").season(1)
    t = s1.torrent({"银八 S01E01.mkv": 600_000_000, "NCOP1.mkv": 90_000_000},
                   name="[G] Gintama BD", layout="original", state="downloading", progress=0.5)
    lib.qbit.fail("set_file_priority", hash=t.hash, after=True)

    c = lib.cycle(detectors=[ExtrasDetector])

    [rec] = c.applied("trash")
    assert rec["priority_zeroed"] and rec["undo"]["op"] == "restore_file_priority"
    assert "ReadTimeout" in rec["confirmed_after_error"]
