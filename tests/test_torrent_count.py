"""种子数合理性：`torrents()` 成功了、却比上一轮少了一大截，而审计里没有对应的摘除——按"数据不完整"整轮拒绝。

第 1 阶段只挡住了"登录成功却 0 个种子"（`scan` 的空列表检查，不需要跨轮状态）。部分缺失——qBit 容器重建时
BT_backup 只恢复了一部分、会话里只剩 300 个而不是 539 个——在接口上同样没有任何报错；而后果与 LAT-01 一样：
缺了种子的文件全成了"纯本地文件"，改名走文件系统、隔离跳过种子。

判据：`掉了的个数 − 审计里记着的本项目摘除数 > max(TORRENT_DROP_MIN=20, ⌈上一轮 × TORRENT_DROP_PCT=10%⌉)`。
"上一轮"是最近一次**被采信**的 `run` 的计数（`state/health/torrent-count.json`）——被拒绝的那一轮不挪基线，
否则第二轮就会拿残缺的数当基准放行。人为的批量删除（在 qBit 里手动删）审计里没有：
`media-agent health --accept-torrent-count` 把此刻的数认作新基线。
"""
from __future__ import annotations

import argparse
import json

import pytest

from media_agent import cli, health
from media_agent.config import load_config

GB = 600_000_000


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _library(lib, healthy=30, dead=0):
    s1 = lib.show("测试番").season(1)
    alive = [s1.single(f"测试番 S01E{i:02d}.mkv", size=GB + i,
                       name=f"[G] Test Show - {i:02d} [1080p].mkv")
             for i in range(1, healthy + 1)]
    gone = [s1.single(f"测试番 S02E{i:02d}.mkv", size=GB + 100 + i,
                      name=f"[G] Test Show S2 - {i:02d} [1080p].mkv", progress=0.0,
                      state="stalledDL", added_hours_ago=24 * 30, availability=0,
                      num_complete=0)
            for i in range(1, dead + 1)]
    return alive, gone


def _vanish(lib, torrents):
    """带外删掉（不经本项目、审计里没有）——qBit 会话只恢复了一部分的形态。"""
    lib.qbit.delete([t.hash for t in torrents], delete_files=False)


def test_first_run_has_no_baseline_and_records_one(offline_cli, capsys):
    lib = offline_cli
    _library(lib, healthy=30)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    base = health.load_baseline(lib.cfg.state_dir)
    assert base["count"] == 30 and base["source"] == "run" and base["run_id"]


@pytest.mark.allow("log_failure", match="不可信")
def test_unexplained_big_drop_fails_the_run_closed(offline_cli, capsys):
    lib = offline_cli
    alive, _ = _library(lib, healthy=30)
    cli.cmd_run(_args(), lib.cfg)
    _vanish(lib, alive[:25])                       # 30 → 5，审计里一条摘除都没有
    # 没了种子的文件成了"纯本地文件"：放行的话，这一个会被文件系统改名（LAT-01 的形态）
    alive[0].path.rename(alive[0].path.with_name("[G] Test Show - 01 [1080p].mkv"))
    before = lib.snapshot()

    rc = cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr()
    assert rc == cli.EXIT_DEGRADED
    assert "拒绝" in out.out and "30" in out.out and "--accept-torrent-count" in out.out
    assert lib.snapshot() == before                # 一个字节都不改
    assert health.load_baseline(lib.cfg.state_dir)["count"] == 30   # 被拒绝的一轮不挪基线


@pytest.mark.allow("log_failure", match="不可信")
def test_the_next_run_with_the_same_partial_count_still_trips(offline_cli, capsys):
    """被拒绝的那一轮若挪了基线，第二轮就会拿残缺的 5 当基准放行。"""
    lib = offline_cli
    alive, _ = _library(lib, healthy=30)
    cli.cmd_run(_args(), lib.cfg)
    _vanish(lib, alive[:25])

    assert cli.cmd_run(_args(), lib.cfg) == cli.EXIT_DEGRADED
    assert cli.cmd_run(_args(), lib.cfg) == cli.EXIT_DEGRADED


def test_legitimate_mass_removal_recorded_in_the_audit_passes(offline_cli, capsys):
    """本项目自己摘掉 25 个死种（drop_torrent，审计里一条一条记着）：30 → 5 是解释得通的。"""
    lib = offline_cli
    _library(lib, healthy=5, dead=25)

    assert cli.cmd_run(_args(), lib.cfg) == 0
    assert sum(1 for r in lib.audit() if r["op"] == "drop_torrent"
               and r["status"] == "applied") == 25
    assert len(lib.qbit.torrents()) == 5

    assert cli.cmd_run(_args(), lib.cfg) == 0
    assert health.load_baseline(lib.cfg.state_dir)["count"] == 5


def test_drop_within_the_threshold_is_fine(offline_cli, capsys):
    lib = offline_cli
    alive, _ = _library(lib, healthy=30)
    cli.cmd_run(_args(), lib.cfg)
    _vanish(lib, alive[:20])                       # 20 ≤ max(20, 3)

    assert cli.cmd_run(_args(), lib.cfg) == 0


def test_percentage_threshold_applies_to_big_libraries(tmp_path, lib):
    """539 个种子：10% 是 54 个，比 20 大——掉 50 个不算异常，掉 60 个算。"""
    cfg = lib.cfg
    health.save_baseline(cfg.state_dir, count=539, run_id="r0", ts="2026-09-26T00:00:00",
                         source="run")
    assert health.torrent_count_problem(cfg, 489) == ""
    assert "539" in health.torrent_count_problem(cfg, 479)


def test_removals_since_the_baseline_count_every_audit_shape(lib):
    """生产审计里摘掉种子记录的几种形状都要认：drop_torrent、整种子隔离（undo.torrent_record_lost）、
    幻影只摘记录（dropped）、搬文件失败但种子已摘（torrent_record_lost）、未确认但发出过 qbit.delete。
    预演的、基线之前的、跳过的不算。"""
    cfg = lib.cfg
    recs = [
        {"ts": "2026-09-26T01:00:00", "status": "applied", "op": "drop_torrent",
         "args": {"torrent_hash": "a"}, "dry_run": False},
        {"ts": "2026-09-26T01:00:00", "status": "applied", "op": "trash",
         "args": {"torrent_hash": "b"}, "undo": {"torrent_record_lost": True}, "dry_run": False},
        {"ts": "2026-09-26T01:00:01", "status": "applied", "op": "trash",
         "args": {"torrent_hash": "c", "phantom": True}, "dropped": "x", "dry_run": False},
        {"ts": "2026-09-26T01:00:02", "status": "failed", "op": "trash",
         "args": {"torrent_hash": "d"}, "torrent_record_lost": True, "dry_run": False},
        {"ts": "2026-09-26T01:00:03", "status": "unknown", "op": "drop_torrent",
         "args": {"torrent_hash": "e"}, "effects_attempted": ["qbit.delete"], "dry_run": False},
        # 不算的：
        {"ts": "2026-09-26T01:00:04", "status": "skipped", "op": "drop_torrent",
         "args": {"torrent_hash": "f"}, "dry_run": False},
        {"ts": "2026-09-26T01:00:05", "status": "applied", "op": "drop_torrent",
         "args": {"torrent_hash": "g"}, "dry_run": True},
        {"ts": "2026-09-25T23:59:59", "status": "applied", "op": "drop_torrent",
         "args": {"torrent_hash": "h"}, "dry_run": False},
        {"ts": "2026-09-26T01:00:06", "status": "applied", "op": "trash",
         "args": {"torrent_hash": "i", "file_only": True},
         "undo": {"torrent_record_lost": False}, "dry_run": False},
    ]
    cfg.audit_log.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    assert health.torrent_removals(cfg.audit_log, "2026-09-26T00:00:00") == 5


def test_accept_command_resets_the_baseline(offline_cli, capsys):
    lib = offline_cli
    alive, _ = _library(lib, healthy=30)
    cli.cmd_run(_args(), lib.cfg)
    _vanish(lib, alive[:25])                       # 人在 qBit 里手动删了 25 个
    capsys.readouterr()

    assert cli.cmd_health(_args(accept_torrent_count=True), lib.cfg) == 0

    out = capsys.readouterr().out
    assert "5" in out and "30" in out
    base = health.load_baseline(lib.cfg.state_dir)
    assert base["count"] == 5 and base["source"] == "accepted"
    assert cli.cmd_run(_args(), lib.cfg) == 0


def test_accept_command_refuses_without_qbit(offline_cli, capsys):
    lib = offline_cli
    _library(lib, healthy=3)
    lib.qbit_down()
    assert cli.cmd_health(_args(accept_torrent_count=True), lib.cfg) == cli.EXIT_DEGRADED
    assert health.load_baseline(lib.cfg.state_dir) is None


def test_broken_baseline_file_is_ignored_not_fatal(lib):
    d = health.health_dir(lib.cfg.state_dir)
    d.mkdir(parents=True)
    (d / health.BASELINE_NAME).write_text("{坏", encoding="utf-8")
    assert health.load_baseline(lib.cfg.state_dir) is None
    assert health.torrent_count_problem(lib.cfg, 1) == ""


def test_thresholds_are_configurable_and_validated(monkeypatch):
    cfg = load_config()
    assert (cfg.torrent_drop_min, cfg.torrent_drop_pct) == (20, 10.0)
    monkeypatch.setenv("TORRENT_DROP_MIN", "5")
    monkeypatch.setenv("TORRENT_DROP_PCT", "2.5")
    cfg = load_config()
    assert (cfg.torrent_drop_min, cfg.torrent_drop_pct) == (5, 2.5)
    for k, bad in (("TORRENT_DROP_MIN", "-1"), ("TORRENT_DROP_PCT", "120"),
                   ("TORRENT_DROP_PCT", "x")):
        monkeypatch.setenv(k, bad)
        with pytest.raises(ValueError, match=k):
            load_config()
        monkeypatch.setenv(k, "5")
