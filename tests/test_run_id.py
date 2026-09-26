"""批次 ID 不撞车（critic N10）。

批次 ID 是回退的单元。以前只精确到秒：同一秒里起的两个 Executor（两个进程，
或将来同一进程里的两轮）共用一个 ID，`rollback` 会把两批改动当成一批一起撤掉。
现在是 `YYYYMMDDTHHMMSS.mmm-<pid>`，旧 ID 继续可用。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import pytest
from harness import video

from media_agent import actions as actions_mod
from media_agent.actions import Executor, new_run_id

LOLI = "[LoliHouse] Yani Neko - {:02d} [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
FORMAT = re.compile(r"^\d{8}T\d{6}\.\d{3}-\d+$")


class _Frozen(datetime):
    """时钟停在 2026-09-26 13:15:02.123456——两个批次落在同一毫秒。"""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 26, 13, 15, 2, 123456)


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(actions_mod, "datetime", _Frozen)
    monkeypatch.setattr(actions_mod, "_last_run_at", None)


def test_format_is_sortable_timestamp_plus_pid():
    rid = new_run_id()
    assert FORMAT.match(rid), rid
    assert rid.endswith(f"-{os.getpid()}")
    at = datetime.strptime(rid.split("-")[0], "%Y%m%dT%H%M%S.%f")
    assert abs((datetime.now() - at).total_seconds()) < 5


def test_ids_from_one_process_are_unique_and_increasing():
    ids = [new_run_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert ids == sorted(ids)                            # 字典序 = 生成顺序


def test_same_millisecond_and_frozen_clock_do_not_collide_or_hang(frozen_clock):
    a, b, c = new_run_id(), new_run_id(), new_run_id()
    assert (a, b, c) == (f"20260926T131502.123-{os.getpid()}",
                         f"20260926T131502.124-{os.getpid()}",
                         f"20260926T131502.125-{os.getpid()}")


def test_other_process_in_the_same_millisecond_gets_its_own_id(frozen_clock, monkeypatch):
    mine = new_run_id()
    monkeypatch.setattr(actions_mod, "_last_run_at", None)          # 另一个进程：各自从零开始
    monkeypatch.setattr(actions_mod.os, "getpid", lambda: 999_999)
    theirs = new_run_id()
    assert mine != theirs
    assert mine.split("-")[0] == theirs.split("-")[0]               # 时间部分一模一样，靠 pid 区分


def test_two_batches_in_the_same_second_roll_back_separately(lib, frozen_clock):
    """旧格式下这两批的 ID 都是 `20260926T131502`：回退第二批会连第一批一起撤掉。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    ex1 = Executor(lib.context(), dry_run=False)
    ex1.apply(lib.diagnose())
    s1.single(LOLI.format(9), size=593_601_177, probe=SUBS)
    ex2 = Executor(lib.context(), dry_run=False)
    ex2.apply(lib.diagnose())
    assert _Frozen.now().strftime("%Y%m%dT%H%M%S") == "20260926T131502"   # 旧格式会撞
    assert ex1.run_id != ex2.run_id
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176,
                          "尼古喵喵/Season 1/尼古喵喵 S01E09.mkv": 593_601_177}

    res = lib.rollback(ex2.run_id)

    assert res["failed"] == 0 and res["skipped"] == 0
    assert res["total"] == len(ex2.report.applied)       # 只取到第二批自己的记录
    assert res["reverted"] == len([r for r in ex2.report.applied if r.get("undo")])
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176,   # 第一批不动
                          f"尼古喵喵/Season 1/{LOLI.format(9)}": 593_601_177}


def test_old_and_new_ids_coexist_in_one_audit_log(lib):
    """生产 audit.jsonl 里有 9,619 行旧格式 ID；新旧混排时列表与按 ID 取记录都照常。"""
    recs = [
        ("2026-09-20T17:01:26", "20260920T170126", "old"),
        ("2026-09-26T13:15:02", "20260926T131502.123-4821", "new-a"),
        ("2026-09-26T13:15:02", "20260926T131502.456-4822", "new-b"),   # 同一秒、另一个进程
    ]
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fh:
        for ts, rid, kind in recs:
            fh.write(json.dumps({"ts": ts, "run_id": rid, "status": "applied", "dry_run": False,
                                 "kind": kind, "op": "retag", "args": {}, "summary": kind,
                                 "undo": {"op": "retag", "torrent_hash": "a" * 40,
                                          "tags": ""}}) + "\n")
    ex = Executor(lib.context(), dry_run=True, run_id="list")

    runs = ex.list_runs()

    assert [r["run_id"] for r in runs] == [rid for _, rid, _ in recs]
    assert [r["kinds"] for r in runs] == [["old"], ["new-a"], ["new-b"]]
    assert [r["kind"] for r in ex._read_audit("20260920T170126")] == ["old"]
    assert [r["kind"] for r in ex._read_audit("20260926T131502.123-4821")] == ["new-a"]
    assert ex._read_audit("20260926T131502") == []       # 旧式前缀不会把新 ID 的批次吞进来
