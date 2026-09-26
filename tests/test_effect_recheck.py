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
