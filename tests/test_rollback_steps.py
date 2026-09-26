"""回退逐步留审计（critic N12）。

以前 `rollback` 只写一条汇总（`status: rollback` + 计数）：它还原了哪几条、跳过了哪几条、哪一步出错，
审计里查不到；健康摘要看不见回退改了什么，回退半路出错也不知道停在哪。现在每一步逆操作一条记录：

- `run_id` = `rollback-of-<被回退的批次>`，`rollback_of` = 被回退的批次，`rollback_id` = 这一次回退自己的 ID
  （同一批可以回退不止一次）；
- `op` = `undo:<逆操作>`，`args` = 逆操作本身，`undoes` 指回原记录（`seq` / `ts` / `op` / `status`）；
- `status`：还原了 `applied`、核对后跳过 `skipped`（`reason`）、没动就出错 `failed`、动了之后出错 `unknown`。

汇总记录照旧：`run_id` 是被回退的批次（critic §2 的更正：`**result` 一直把字面量 `rollback-of-…`
覆盖成了原批次号，生产上 4 条历史回退汇总也是这个形状），`list_runs` 靠它标"已回退"。
回退记录本身不带逆操作（回退不能再回退），`rollback --last` 不会选到回退批次。
"""
from __future__ import annotations

import argparse
import json

from harness import video

from media_agent import cli
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding

LOLI = "[LoliHouse] Yani Neko - {:02d} [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _two_renames(lib):
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    b = s1.single(LOLI.format(9), size=593_601_177, probe=SUBS)
    return s1, a, b


def _steps(lib, run_id: str) -> list[dict]:
    return lib.audit(f"rollback-of-{run_id}")


def test_each_undo_step_is_audited_and_linked_to_what_it_undoes(lib):
    _two_renames(lib)
    c = lib.cycle()
    forward = {r["seq"]: r for r in lib.audit(c.run_id)}
    undoable = [r for r in forward.values() if r.get("undo")]

    res = lib.rollback(c.run_id)

    steps = _steps(lib, c.run_id)
    assert len(steps) == len(undoable) == res["reverted"]
    for st in steps:
        assert st["rollback_of"] == c.run_id and st["status"] == "applied"
        assert st["rollback_id"] == f"rb-{c.run_id}"
        orig = forward[st["undoes"]["seq"]]
        assert st["op"] == f"undo:{orig['undo']['op']}" and st["args"] == orig["undo"]
        assert st["undoes"] == {"seq": orig["seq"], "ts": orig["ts"], "op": orig["op"],
                                "status": "applied"}
        assert "undo" not in st                           # 回退不能再回退
    # LIFO：后执行的先还原
    assert [st["undoes"]["seq"] for st in steps] == sorted((r["seq"] for r in undoable), reverse=True)
    # 汇总照旧：run_id 是被回退的那一批，list_runs 靠它标"已回退"
    [summary] = [r for r in lib.audit(c.run_id) if r["status"] == "rollback"]
    assert summary["reverted"] == res["reverted"]
    assert summary["rollback_run_id"] == f"rollback-of-{c.run_id}"


def test_skipped_step_records_why(lib):
    _, a, _ = _two_renames(lib)
    c = lib.cycle()
    # 回退之前有人把 E08 又改了名：逆改名核对"当前文件不在"而跳过
    lib.qbit.rename_file(a.hash, "尼古喵喵 S01E08.mkv", "别人改的.mkv")

    lib.rollback(c.run_id)

    [st] = [x for x in _steps(lib, c.run_id) if x["args"].get("torrent_hash") == a.hash]
    assert st["status"] == "skipped" and "不存在" in st["reason"]


def test_step_that_raised_after_issuing_a_change_is_unknown_not_failed(lib):
    """目录改名的逆操作：setLocation 超时（qBittorrent 其实已经受理）。汇总照旧把它算进 failed
    （"这一步抛了异常"），逐步记录说实话：它发出过改动，结局未确认。"""
    show = lib.show("旧名")
    t = show.season(1).single("旧名 S01E01.mkv", size=1000)
    f = Finding(rule="title-drift", kind="title_drift", severity="important", summary="改目录名",
                show="旧名", path=str(show.path),
                action=Action(op="rename_show_dir", args={"path": str(show.path), "new_name": "新名"}))
    rep = lib.apply([f], run_id="t-dir")
    assert rep.applied
    lib.qbit.fail("set_location", hash=t.hash, after=True)

    res = lib.rollback("t-dir")

    assert res["failed"] == 1
    [st] = _steps(lib, "t-dir")
    assert st["status"] == "unknown" and "setLocation" in st["error"]
    assert st["effects_attempted"] == ["qbit.set_location"]


def test_step_that_raised_before_any_change_is_failed(lib, monkeypatch):
    _, a, _ = _two_renames(lib)
    c = lib.cycle()
    real = Executor._apply_undo

    def boom(self, u, rec=None):
        if u.get("torrent_hash") == a.hash:
            raise RuntimeError("bug in undo (injected)")
        return real(self, u, rec)

    monkeypatch.setattr(Executor, "_apply_undo", boom)

    res = lib.rollback(c.run_id)

    assert res["failed"] == 1
    [st] = [x for x in _steps(lib, c.run_id) if x["args"].get("torrent_hash") == a.hash]
    assert st["status"] == "failed" and "bug in undo" in st["error"]
    assert "effects_attempted" not in st


def test_dry_run_rollback_writes_nothing(lib):
    _two_renames(lib)
    c = lib.cycle()
    before = lib.cfg.audit_log.read_text(encoding="utf-8")

    lib.rollback(c.run_id, dry_run=True)

    assert lib.cfg.audit_log.read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ runs / --last
def test_list_runs_marks_the_rolled_back_run_and_labels_the_rollback_batch(lib):
    _two_renames(lib)
    c = lib.cycle()
    lib.rollback(c.run_id)

    runs = {r["run_id"]: r for r in Executor(lib.context()).list_runs()}

    assert runs[c.run_id]["rolled_back"] is True
    rb = runs[f"rollback-of-{c.run_id}"]
    assert rb["rollback_of"] == c.run_id and rb["undoable"] == 0
    assert not rb.get("rolled_back")


def test_rollback_last_skips_rollback_batches_and_rolled_back_runs(lib, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    first = lib.cycle()
    s1.single(LOLI.format(9), size=593_601_177, probe=SUBS)
    second = lib.cycle()
    lib.rollback(second.run_id)

    assert cli.cmd_runs(argparse.Namespace(), lib.cfg) == 0
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if x.startswith(f"rollback-of-{second.run_id}")]
    assert f"回退 {second.run_id}" in line

    rc = cli.cmd_rollback(argparse.Namespace(run=None, last=True, dry_run=True), lib.cfg)
    assert rc == 0 and f"批次 {first.run_id}" in capsys.readouterr().out


def test_rolling_back_a_rollback_batch_is_refused(lib, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    _two_renames(lib)
    c = lib.cycle()
    lib.rollback(c.run_id)
    before = lib.snapshot()

    rc = cli.cmd_rollback(argparse.Namespace(run=f"rollback-of-{c.run_id}", last=False,
                                             dry_run=False), lib.cfg)

    assert rc == cli.EXIT_DEGRADED
    assert "回退记录" in capsys.readouterr().out
    assert lib.snapshot() == before


def test_historical_rollback_summary_still_marks_its_run(lib):
    """生产上的 4 条历史回退汇总（2026-08-17 … 08-31）：只有 ts / run_id / status 与计数。"""
    rec = {"ts": "2026-08-31T15:03:00", "run_id": "20260831T150040", "status": "rollback",
           "total": 25, "reverted": 25, "skipped": 0, "failed": 0, "irreversible": 0,
           "torrent_records_lost": 0, "skipped_detail": [], "failed_detail": []}
    fwd = {"ts": "2026-08-31T15:00:41", "run_id": "20260831T150040", "status": "applied",
           "dry_run": False, "rule": "r", "kind": "k", "op": "rename", "args": {},
           "summary": "x", "undo": {"op": "rename"}}
    lib.cfg.audit_log.write_text(json.dumps(fwd) + "\n" + json.dumps(rec) + "\n", encoding="utf-8")

    [run] = Executor(lib.context()).list_runs()

    assert run["run_id"] == "20260831T150040" and run["rolled_back"] is True
