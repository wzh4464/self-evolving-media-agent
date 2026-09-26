"""`media-agent subscribe`：不经 AutoBangumi 订阅一部番的一季。

AB 以后仍是订阅的前端，可 AB 不在（或不想经 AB）时也要能订：`--tmdb ID [--season N] [--mikan ID] [--dir NAME]
[--require-any WORD …] [--offset N]`。它建番目录 + sidecar（人的意图：订阅、TMDB 身份、番组页、版本要求、集号偏移），
**经同一套动作与审计**（`create_show_dir` / `subscribe_season`，能 `rollback`），然后说出下一次抓取会做什么。
已有这部番的目录（sidecar 的 tmdb_id）就登记进那个目录，不另建一个；和已有的人写的东西冲突就拒绝、什么都不写。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import pytest
from harness import MikanItem, weekly

from media_agent import cli
from media_agent import sidecar as sc_mod
from media_agent.runlock import LOCK_NAME, RunLock

TMDB = 3601
TITLE = "新番丁"
MID = "4801"
TPL = "[LoliHouse] 新番丁 / Shinban Tei - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def _args(**kw) -> argparse.Namespace:
    base = dict(tmdb=TMDB, season=None, mikan=None, dir=None, require_any=None, offset=None,
                dry_run=False, no_tmdb=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _airing(lib, *, seasons=None, eps=(1, 2)):
    sched = weekly(12, first_days_ago=10)
    lib.tmdb.add_show(TMDB, TITLE, seasons=seasons or {1: sched})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in eps], search=[TITLE])
    return sched


def _sidecar(lib, name=TITLE) -> dict:
    return json.loads(sc_mod.path_for(lib.path(name)).read_text(encoding="utf-8"))


def test_subscribing_a_new_show_creates_the_dir_through_the_executor(offline_cli, capsys):
    lib = offline_cli
    _airing(lib)

    rc = cli.cmd_subscribe(_args(season=1, mikan=MID), lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0, out
    d = lib.path(TITLE)
    assert (d / "Season 1").is_dir()
    sc = _sidecar(lib)
    assert (sc["tmdb_id"], sc["tmdb_source"], sc["mikan_id"]) == (TMDB, "human", MID)
    assert sc["subscriptions"] == {"1": {"source": "cli", "since": date.today().isoformat(), "mikan_id": MID}}
    [rec] = [r for r in lib.audit() if r["op"] == "create_show_dir"]
    assert rec["status"] == "applied" and rec["rule"] == "subscribe"
    # 下一次抓取会做什么：两集可抓
    assert "S01E01" in out and "S01E02" in out and "可抓取" in out


def test_the_default_season_is_the_latest_on_tmdb(offline_cli):
    lib = offline_cli
    _airing(lib, seasons={1: weekly(12, first_days_ago=800), 2: weekly(12, first_days_ago=10)})

    assert cli.cmd_subscribe(_args(), lib.cfg) == 0

    assert set(_sidecar(lib)["subscriptions"]) == {"2"}
    assert (lib.path(TITLE) / "Season 2").is_dir()


def test_user_intent_flags_land_in_the_sidecar(offline_cli):
    lib = offline_cli
    _airing(lib)

    rc = cli.cmd_subscribe(_args(season=1, require_any=["邪竜解放版", "邪龙解放版"], offset=-12), lib.cfg)

    assert rc == 0
    sc = _sidecar(lib)
    assert sc["require_any"] == ["邪竜解放版", "邪龙解放版"] and sc["episode_offsets"] == {"1": -12}


def test_an_existing_dir_for_the_show_gets_the_season_not_a_twin(offline_cli):
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    _airing(lib, seasons={1: weekly(12, first_days_ago=800), 2: weekly(12, first_days_ago=10)})
    sh = lib.show("新番丁（旧名）")
    sh.season(1).local("新番丁（旧名） S01E01.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", notes=["人写的"])
    lib.web.mikan_search("新番丁（旧名）", [MID])          # 抓取也按目录名（规范名）搜番组页

    assert cli.cmd_subscribe(_args(season=2, mikan=MID), lib.cfg) == 0

    assert not lib.path(TITLE).exists()
    sc = lib.sidecar("新番丁（旧名）")
    assert sc.subscriptions["2"]["source"] == "cli" and sc.notes == ["人写的"]
    [rec] = [r for r in lib.audit() if r["op"] == "subscribe_season"]
    assert rec["status"] == "applied"


def test_a_custom_dir_name(offline_cli):
    lib = offline_cli
    _airing(lib)
    lib.web.mikan_search("Shinban Tei", [MID])

    assert cli.cmd_subscribe(_args(season=1, dir="Shinban Tei"), lib.cfg) == 0

    assert (lib.path("Shinban Tei") / "Season 1").is_dir()
    assert not lib.path(TITLE).exists()


@pytest.mark.parametrize("existing, flags, needle", [
    ({"require_any": ["TV版"]}, {"require_any": ["邪竜解放版"]}, "require_any"),
    ({"episode_offsets": {"1": -24}}, {"offset": -12}, "episode_offsets"),
    ({"tmdb_id": 9999, "tmdb_source": "human"}, {}, "tmdb_id"),
])
def test_a_conflict_with_what_a_human_wrote_refuses_everything(offline_cli, capsys, existing, flags, needle):
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    _airing(lib)
    sh = lib.show(TITLE)
    sh.season(1).local(f"{TITLE} S01E01.mkv")
    sh.sidecar(**{"tmdb_id": TMDB, "tmdb_source": "human", **existing})
    before = sc_mod.path_for(sh.path).read_bytes()

    rc = cli.cmd_subscribe(_args(season=1, **flags), lib.cfg)

    assert rc == 2
    assert needle in capsys.readouterr().out
    assert sc_mod.path_for(sh.path).read_bytes() == before
    assert not lib.audit()


def test_two_dirs_for_one_show_ask_for_dir(offline_cli, capsys):
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    _airing(lib)
    for name in ("新番丁 A", "新番丁 B"):
        sh = lib.show(name)
        sh.season(1).local(f"{name} S01E01.mkv")
        sh.sidecar(tmdb_id=TMDB, tmdb_source="human")

    assert cli.cmd_subscribe(_args(season=1), lib.cfg) == 2
    assert "--dir" in capsys.readouterr().out


@pytest.mark.allow("tmdb_unknown")                       # 这里就是要问一个不存在的 id
def test_an_unknown_tmdb_id_is_refused(offline_cli, capsys):
    lib = offline_cli
    lib.tmdb.enabled = True

    rc = cli.cmd_subscribe(_args(tmdb=424242, season=1), lib.cfg)

    assert rc == 2 and "424242" in capsys.readouterr().out
    assert not list(lib.media_root.iterdir())


def test_dry_run_writes_nothing(offline_cli, capsys):
    lib = offline_cli
    _airing(lib)

    assert cli.cmd_subscribe(_args(season=1, dry_run=True), lib.cfg) == 0

    assert not lib.path(TITLE).exists()
    assert "预演" in capsys.readouterr().out


def test_an_already_subscribed_season_is_a_no_op(offline_cli, capsys):
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    _airing(lib)
    sh = lib.show(TITLE)
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", subscriptions={"1": {"source": "autobangumi"}})

    assert cli.cmd_subscribe(_args(season=1), lib.cfg) == 0

    assert "已经订阅" in capsys.readouterr().out
    assert not lib.audit()


def test_a_season_that_is_not_airing_is_explained(offline_cli, capsys):
    """老番的某一季：订阅照样记下，但抓取只补在播的季——说清楚下一次抓取不会抓它。"""
    lib = offline_cli
    lib.tmdb.add_show(TMDB, TITLE, seasons={1: weekly(12, first_days_ago=900)})

    assert cli.cmd_subscribe(_args(season=1), lib.cfg) == 0

    out = capsys.readouterr().out
    assert "不在播" in out and "不会抓" in out


def test_rollback_undoes_the_subscription(offline_cli):
    lib = offline_cli
    _airing(lib)
    assert cli.cmd_subscribe(_args(season=1), lib.cfg) == 0
    [rec] = [r for r in lib.audit() if r["op"] == "create_show_dir"]

    res = lib.rollback(rec["run_id"])

    assert res["reverted"] == 1 and not lib.path(TITLE).exists()


def test_the_subcommand_is_wired_and_takes_the_run_lock(offline_cli, monkeypatch):
    lib = offline_cli
    monkeypatch.setattr(cli, "load_config", lambda: lib.cfg)
    monkeypatch.setattr(cli.runlock, "DEFAULT_WAIT", 0.1)
    holder = RunLock(lib.cfg.state_dir / LOCK_NAME, "media-agent run")
    assert holder.acquire(wait=0)
    monkeypatch.setattr(sys, "argv", ["media-agent", "subscribe", "--tmdb", str(TMDB), "--season", "1",
                                      "--require-any", "简", "繁"])
    try:
        assert cli.main() == cli.EXIT_LOCKED
    finally:
        holder.release()
    assert not lib.path(TITLE).exists()
