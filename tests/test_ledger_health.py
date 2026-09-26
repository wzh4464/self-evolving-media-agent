"""健康报告带出处账本的覆盖率（`RunHealth.ledger`）。

- `ledger`：这一轮的种子里有出处的几个、没有的几个（其中加进来超过 24 小时的几个）、本轮补录了什么、账本读不了的原因；
- warn `provenance_unknown_grew`：加进来超过 24 小时还查不到出处的种子**比上一轮多了**——只看增长，不看存量：生产上
  开张时就有二十来个查不到出处的（人手加的合集等），每轮都报就成了噪音；新冒出来一个没人知道来路的种子才值得看；
- warn `ledger_unavailable`：账本读不了（坏了、结构版本更新）——这一轮按没有账本认集位与版本，要人看。账本不拦路：
  退出码照旧。
"""
from __future__ import annotations

import argparse

import pytest

from media_agent import cli, health, ledger

H_AB, H_X, H_Y = "1" * 40, "5" * 40, "6" * 40


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _ab_show(lib):
    sh = lib.show("银八")
    sh.bangumi(38, title_raw="Gintama", season=1)
    lib.ab_rows("torrent", [{"bangumi_id": 38, "name": "[LoliHouse] 3年Z组银八老师 / Gintama - 01 [1080p]",
                             "url": f"https://mikanani.me/Download/20260901/{H_AB}.torrent"}])
    sh.season(1).single("银八 S01E01.mkv", name="[LoliHouse] Gintama - 01 [1080p].mkv", hash=H_AB)


def _codes(rep) -> set[str]:
    return {r["code"] for r in rep["reasons"]}


def test_the_report_carries_ledger_coverage(offline_cli, capsys):
    lib = offline_cli
    _ab_show(lib)
    lib.show("抓娃娃").season(1).single("[X] Zhua Wa Wa - 01.mkv", hash=H_X, added_hours_ago=48)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = health.load_report(lib.cfg.state_dir)
    lg = rep["ledger"]
    assert (lg["torrents"], lg["covered"], lg["unknown"], lg["unknown_old"]) == (2, 1, 1, 1)
    assert lg["problem"] == "" and lg["backfill"]["inserted"] == {ledger.AUTOBANGUMI: 1}
    assert "provenance_unknown_grew" not in _codes(rep)           # 第一轮没有可比的：不报
    out = capsys.readouterr().out
    assert "出处    有出处 1/2" in out


def test_only_growth_of_old_unknown_torrents_warns(offline_cli):
    lib = offline_cli
    _ab_show(lib)
    s = lib.show("抓娃娃").season(1)
    s.single("[X] Zhua Wa Wa - 01.mkv", hash=H_X, added_hours_ago=48)
    cli.cmd_run(_args(), lib.cfg)

    s.single("[X] Zhua Wa Wa - 02.mkv", hash=H_Y, added_hours_ago=30)     # 新冒出来一个没人知道来路的
    cli.cmd_run(_args(), lib.cfg)
    grew = health.load_report(lib.cfg.state_dir)
    cli.cmd_run(_args(), lib.cfg)
    same = health.load_report(lib.cfg.state_dir)

    assert "provenance_unknown_grew" in _codes(grew) and grew["status"] == "warn"
    [r] = [r for r in grew["reasons"] if r["code"] == "provenance_unknown_grew"]
    assert "1 → 2" in r["text"] and H_Y[:8] in r["text"]
    assert "provenance_unknown_grew" not in _codes(same)            # 不再增长：不报


def test_a_fresh_unknown_torrent_does_not_warn_yet(offline_cli):
    """刚加进来的（不到 24 小时）还没来得及登记——AB 下一轮就会补上，不报。"""
    lib = offline_cli
    _ab_show(lib)
    cli.cmd_run(_args(), lib.cfg)
    lib.show("抓娃娃").season(1).single("[X] Zhua Wa Wa - 01.mkv", hash=H_X, added_hours_ago=2)

    cli.cmd_run(_args(), lib.cfg)

    rep = health.load_report(lib.cfg.state_dir)
    assert rep["ledger"]["unknown"] == 1 and rep["ledger"]["unknown_old"] == 0
    assert "provenance_unknown_grew" not in _codes(rep)


def test_an_unreadable_ledger_warns_and_the_run_goes_on(offline_cli):
    lib = offline_cli
    _ab_show(lib)
    ledger.path_of(lib.cfg.state_dir).write_bytes(b"junk" * 200)

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = health.load_report(lib.cfg.state_dir)
    assert rc == 0 and rep["status"] == "warn"
    [r] = [r for r in rep["reasons"] if r["code"] == "ledger_unavailable"]
    assert ledger.LEDGER_NAME in r["text"]
    assert ledger.path_of(lib.cfg.state_dir).read_bytes() == b"junk" * 200
