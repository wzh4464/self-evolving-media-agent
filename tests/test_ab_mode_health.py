"""健康报告里的 AutoBangumi 模式（`abmode`）：每轮 `run` / `grab` 都写出本项目认的模式与来源；订阅模式下核对切换之后 AB
还做了什么（`abmode.activity`，启发式）——`/api/v1/status` 说不出它的线程停没停（ab 调研 §3.2）：

- `ab_still_polling`：rssitem 的 `last_checked_at` 与切换时的基线不同——RSS 线程还在跑（没重启 / 开关被改回）或有人点了
  刷新（刷新也会让它下载）；
- `ab_added_outside_subscribe`：切换之后 AB 加的种子（`Bangumi` 分类 / `ab:<id>` 标签，不钉 `ma:`）不是新订阅那一刻补的那一批；
- `ab_mode_unverified`：没有切换基线（模式来自 `AB_MODE`、没经命令切过），或这一轮读不到 AB 库的 rssitem——什么都核对不了。

都是 warn：run 的通知按状态变化发信。`full` 模式只写模式、不核对。
"""
from __future__ import annotations

import argparse
import sys

import pytest

from media_agent import abmode, cli, health

T0 = "2026-09-27T03:45:00.000000+00:00"
GB = 600_000_000


@pytest.fixture
def ab_cli(lib, monkeypatch):
    sh = lib.show("甲番")
    sh.bangumi(1, title_raw="Jia")
    sh.season(1).single("甲番 S01E01.mkv", size=GB, name="[G] Jia - 01 [1080p].mkv", tags="ab:1")
    lib.ab_rows("rssitem", [{"id": 1, "name": "甲番", "url": "https://mikan.invalid/RSS/Bangumi?bangumiId=1",
                             "last_checked_at": T0}])
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    monkeypatch.setattr(cli, "load_config", lambda: lib.cfg)
    monkeypatch.setattr(abmode, "POLL_S", 0.0)
    lib.tmdb.enabled = True
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _switch(lib, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["media-agent", "ab-mode", "subscription"])
    assert cli.main() == 0
    lib.configure(ab_mode="subscription", ab_mode_source="state")


def _run(lib) -> dict:
    cli.cmd_run(_args(), lib.cfg)
    return health.load_report(lib.cfg.state_dir)


def _codes(rep) -> set:
    return {r["code"] for r in rep["reasons"]}


def test_full_mode_just_records_the_mode(ab_cli, capsys):
    rep = _run(ab_cli)
    assert rep["ab"] == {"mode": "full", "source": "default"}
    assert rep["status"] == "ok"
    assert "AB      full（default）" in capsys.readouterr().out


def test_a_quiet_ab_after_the_switch_is_ok(ab_cli, monkeypatch, capsys):
    lib = ab_cli
    _switch(lib, monkeypatch)
    capsys.readouterr()

    rep = _run(lib)

    ab = rep["ab"]
    assert ab["mode"] == "subscription" and ab["source"] == "state" and ab["since"]
    assert ab["check"]["baseline"] is True and ab["check"]["polled"] == 0 and ab["check"]["outside"] == 0
    assert rep["status"] == "ok", rep["reasons"]
    out = capsys.readouterr().out
    assert "AB      subscription（state" in out and "拉 RSS 0" in out


def test_ab_still_polling_after_the_switch_warns(ab_cli, monkeypatch):
    lib = ab_cli
    _switch(lib, monkeypatch)
    lib.ab.running["rss"] = True                       # 线程其实没停
    lib.ab.poll_rss("2026-09-27T09:00:00+00:00")

    rep = _run(lib)

    assert "ab_still_polling" in _codes(rep) and rep["status"] == "warn"
    assert rep["ab"]["check"]["polled"] == 1
    [r] = [r for r in rep["reasons"] if r["code"] == "ab_still_polling"]
    assert "甲番" in r["text"] and r["level"] == "warn"


def test_a_torrent_ab_added_outside_a_subscribe_warns(ab_cli, monkeypatch):
    """老订阅（切换时就有的 1 号）又加了一集，带 `ab:1`：RSS 线程 / 刷新 / 人点了「收集」——订阅之外它不该再下载。"""
    lib = ab_cli
    _switch(lib, monkeypatch)
    lib.show("甲番").season(1).single("[G] Jia - 02 [1080p].mkv", size=GB + 2, category="Bangumi", tags="ab:1",
                                      added_hours_ago=-0.1)

    rep = _run(lib)

    assert "ab_added_outside_subscribe" in _codes(rep)
    assert rep["ab"]["check"]["outside"] == 1


def test_a_new_subscriptions_collect_is_expected(ab_cli, monkeypatch):
    """人在 AB 里订了一部新番（切换之后才有的 3 号）：订阅那一刻 AB 补进 `Bangumi` 的那一批（还没有 id，不带标签）是正常的。"""
    lib = ab_cli
    _switch(lib, monkeypatch)
    lib.show("乙番").bangumi(3, title_raw="Yi")
    lib.show("乙番").season(1).single("[G] Yi - 01 [1080p].mkv", size=GB + 3, category="Bangumi",
                                      added_hours_ago=-0.1)

    rep = _run(lib)

    assert not _codes(rep) & {"ab_added_outside_subscribe", "ab_still_polling", "ab_mode_unverified"}
    assert rep["ab"]["check"]["subscribe"] == 1


def test_subscription_mode_from_the_env_cannot_be_verified(ab_cli):
    ab_cli.configure(ab_mode="subscription", ab_mode_source="env")
    rep = _run(ab_cli)
    assert rep["ab"]["check"]["baseline"] is False
    assert "ab_mode_unverified" in _codes(rep)


def test_an_unreadable_ab_db_cannot_be_verified(ab_cli, monkeypatch):
    lib = ab_cli
    _switch(lib, monkeypatch)
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context(abdb=None))

    rep = _run(lib)

    [r] = [r for r in rep["reasons"] if r["code"] == "ab_mode_unverified"]
    assert "rssitem" in r["text"]


def test_the_grab_report_carries_the_mode_too(ab_cli, monkeypatch):
    lib = ab_cli
    _switch(lib, monkeypatch)
    lib.ab.running["rss"] = True
    lib.ab.poll_rss("2026-09-27T09:00:00+00:00")

    cli.cmd_grab(argparse.Namespace(dry_run=False, no_tmdb=True), lib.cfg)

    rep = health.load_report(lib.cfg.state_dir, cmd="grab")
    assert rep["ab"]["mode"] == "subscription" and "ab_still_polling" in _codes(rep)
