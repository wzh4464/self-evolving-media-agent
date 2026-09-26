"""维护暂停（critic N17）：VPN 救援进行中（`~/gluetun/.rescue-active`），或有人放了 `state/PAUSE` 时，
`run` / `apply`（以后的抓取模式同样）一开始就以 75 结束、什么都不做；`diagnose` 照常。

为什么：`rescue.py` / `vpn-watchdog.sh` 会重建 qBittorrent 容器；救援期间做种被暂停、分享率上限改成 1.0、流量走
按量计费的 VPS。media-agent 在这时候跑一轮——读到一半的 qBit、刚被暂停的种子、抓取走 VPS——不该发生。

暂停的 `run` 照样写健康报告（warn「维护暂停：…」），通知在 ok → warn 时发一封：一个忘了删的 `state/PAUSE`
不能让 agent 悄悄停摆（这一阶段要解决的正是这个）。暂停不拿运行锁：救援脚本重建容器时自己拿着它。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pytest

from media_agent import cli, health, pause
from media_agent.config import load_config
from media_agent.runlock import LOCK_NAME, RunLock


@pytest.fixture
def marker(tmp_path, monkeypatch):
    m = tmp_path / "gluetun" / ".rescue-active"
    monkeypatch.setenv("RESCUE_MARKER", str(m))
    return m


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["media-agent", *argv])
    return cli.main()


def _no_real_work(monkeypatch):
    for name in ("cmd_run", "cmd_apply", "cmd_diagnose"):
        monkeypatch.setattr(cli, name, lambda args, cfg, n=name: print(f"<{n} 跑了>") or 0)


def test_rescue_marker_pauses_run_and_apply_with_75(monkeypatch, marker, capsys):
    _no_real_work(monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("rescue mode active — vpn-watchdog 请勿接管\n", encoding="utf-8")

    assert _main(monkeypatch, "run") == cli.EXIT_LOCKED == 75
    assert _main(monkeypatch, "apply") == 75

    out, err = capsys.readouterr()
    assert "<cmd_run 跑了>" not in out and "<cmd_apply 跑了>" not in out
    assert "救援" in out and str(marker) in out and "救援" in err


def test_pause_file_pauses_and_says_how_to_resume(monkeypatch, marker, capsys, project_root):
    _no_real_work(monkeypatch)
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "PAUSE").write_text("换硬盘，周日前别动\n", encoding="utf-8")

    assert _main(monkeypatch, "run") == 75

    out = capsys.readouterr().out
    assert "换硬盘，周日前别动" in out and "PAUSE" in out and "删掉" in out


def test_diagnose_still_works_while_paused(monkeypatch, marker, capsys):
    _no_real_work(monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("x")

    assert _main(monkeypatch, "diagnose") == 0
    assert "<cmd_diagnose 跑了>" in capsys.readouterr().out


def test_not_paused_runs_normally(monkeypatch, marker, capsys):
    _no_real_work(monkeypatch)
    assert _main(monkeypatch, "run") == 0
    assert "<cmd_run 跑了>" in capsys.readouterr().out


def test_paused_run_does_not_wait_for_the_lock(monkeypatch, marker, capsys, project_root):
    """救援脚本重建容器时拿着运行锁：暂停的 run 不去等它 10 秒，直接说"暂停中"。"""
    _no_real_work(monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    lock = RunLock(project_root / "state" / LOCK_NAME, "rescue.py start")
    assert lock.acquire(wait=0)
    try:
        t0 = time.monotonic()
        assert _main(monkeypatch, "run") == 75
        assert time.monotonic() - t0 < 5
    finally:
        lock.release()
    assert "救援" in capsys.readouterr().out


def test_paused_run_writes_a_warn_health_report(monkeypatch, marker, capsys, project_root):
    _no_real_work(monkeypatch)
    marker.parent.mkdir(parents=True)
    marker.write_text("x")

    _main(monkeypatch, "run")

    rep = health.load_report(project_root / "state")
    assert rep["status"] == "warn" and rep["exit_code"] == 75
    assert [r["code"] for r in rep["reasons"]] == ["paused"] and "救援" in rep["paused"]


def test_pause_reason_reports_age(tmp_path, marker, project_root):
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    old = time.time() - 3 * 3600
    os.utime(marker, (old, old))
    why = pause.reason(load_config())
    assert "3.0 小时" in why


def test_default_marker_is_under_home_gluetun(monkeypatch, tmp_path):
    monkeypatch.delenv("RESCUE_MARKER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_config().rescue_marker == tmp_path / "gluetun" / ".rescue-active"


def test_library_harness_config_has_no_marker(lib):
    """直接构造的 Config（测试基座）不看救援标记——开发机上真有 ~/gluetun 时测试也不会被暂停。"""
    assert lib.cfg.rescue_marker is None and pause.reason(lib.cfg) == ""


def test_other_mutating_commands_are_marked_pausable_or_not(monkeypatch):
    """`run` / `apply` 暂停；人手动的 rollback / repair / purge / evolve 不拦（维护期间正是人在操作）。"""
    captured = {}

    def grab(args, cfg):
        captured[args.cmd] = getattr(args, "pause", False)
        return 0

    for name in ("cmd_run", "cmd_apply", "cmd_rollback", "cmd_repair", "cmd_purge"):
        monkeypatch.setattr(cli, name, grab)
    for argv in (["run"], ["apply"], ["rollback", "--dry-run"], ["repair", "--run", "x"],
                 ["purge"]):
        _main(monkeypatch, *argv)
    assert captured == {"run": True, "apply": True, "rollback": False, "repair": False,
                        "purge": False}


def test_locked_out_run_writes_a_warn_health_report(monkeypatch, marker, capsys, project_root):
    """运行锁被占的一轮同样什么都没做：以前只打一行、退出码 75、没有健康报告。锁若被一个卡死的进程一直拿着，
    每一轮都这样悄悄结束——正是这一阶段要让人看见的停摆。现在写 warn 报告（持有者是谁、从什么时候起）。"""
    _no_real_work(monkeypatch)
    monkeypatch.setattr(cli.runlock, "DEFAULT_WAIT", 0.1)
    lock = RunLock(project_root / "state" / LOCK_NAME, "media-agent purge --apply")
    assert lock.acquire(wait=0)
    try:
        assert _main(monkeypatch, "run") == 75
    finally:
        lock.release()

    rep = health.load_report(project_root / "state")
    assert rep["status"] == "warn" and rep["exit_code"] == 75
    [r] = rep["reasons"]
    assert r["code"] == "locked" and "media-agent purge --apply" in r["text"]
    assert "<cmd_run 跑了>" not in capsys.readouterr().out
