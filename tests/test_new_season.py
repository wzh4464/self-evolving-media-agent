"""订阅着的番下一季开播时，登记新一季的订阅（`new-season` → `subscribe_season`）。

AB 的订阅是一季一行：新一季要人在 AB 里再订一次（`autobangumi-subscribe-verify` 流程专门讲 "Season N" 续作）。AB 退役之后
没有这一步，老番的新一季就不会来（critic §4）。现在：这部番是订阅着的（sidecar 有 `subscriptions`，或 AB 里有它的有效订阅），
TMDB 上它的下一季已经开播 / 一周内开播、而且是在播的季（`is_seasonal`）→ 登记下一季的订阅，抓取接着抓。

不登记：没订阅的番（人不追了的，哪怕档案里还留着 AB 的旧 id）、下一季还早、下一季是老早以前播的、库内编号与 TMDB
对不上的（压平成一季的 `season_offsets`、库里一季的集数比 TMDB 那一季还多）。
"""
from __future__ import annotations

from datetime import date, timedelta

from harness import MikanItem, weekly

from media_agent.plugins.grab import EpisodeAvailableDetector
from media_agent.plugins.new_season import NEW_SEASON_LEAD_DAYS, NewSeasonDetector

SHOW = "续作丙"
TMDB = 3501
MID2 = "4702"
TPL2 = "[LoliHouse] 续作丙 第二季 / Zokusaku Hei S2 - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _show(lib, *, s2_first_days_ago: int | None = 3, ab: bool = True, sidecar: dict | None = None,
          s1_have=range(1, 13), s1_count: int = 12):
    """第一季早播完、盘上收齐；第二季 `s2_first_days_ago` 天前开播（负数 = 还有几天；None = TMDB 上还没有第二季）。"""
    lib.configure(qbit_allow_empty=True)
    seasons = {1: weekly(s1_count, first_days_ago=600)}
    if s2_first_days_ago is not None:
        seasons[2] = weekly(12, first_days_ago=s2_first_days_ago)
    lib.tmdb.add_show(TMDB, SHOW, seasons=seasons)
    sh = lib.show(SHOW)
    for n in s1_have:
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW,
               seasons={"1": {"have": list(s1_have)}}, **(sidecar or {}))
    if ab:
        sh.bangumi(61, title_raw="Zokusaku Hei", season=1)
    return sh


def _proposals(lib):
    return [f for f in lib.diagnose(detectors=[NewSeasonDetector]) if f.action]


def test_the_next_season_of_an_ab_subscribed_show_is_registered(lib):
    _show(lib)

    [f] = _proposals(lib)

    assert (f.rule, f.kind, f.subject) == ("new-season", "new_season", "S02")
    assert f.action.op == "subscribe_season" and f.action.args["season"] == 2
    sub = f.action.args["subscription"]
    assert sub["source"] == "new-season" and sub["after"] == 1 and sub["tmdb_id"] == TMDB
    assert sub["first_air"] == (date.today() - timedelta(days=3)).isoformat()


def test_a_season_starting_within_the_lead_is_registered_later_ones_are_not(lib):
    _show(lib, s2_first_days_ago=-(NEW_SEASON_LEAD_DAYS - 2))
    assert [f.subject for f in _proposals(lib)] == ["S02"]


def test_a_season_starting_later_than_the_lead_waits(lib):
    _show(lib, s2_first_days_ago=-(NEW_SEASON_LEAD_DAYS + 20))
    assert not _proposals(lib)


def test_a_show_subscribed_through_the_sidecar_counts(lib):
    _show(lib, ab=False, sidecar={"subscriptions": {"1": {"source": "cli"}}})
    assert [f.subject for f in _proposals(lib)] == ["S02"]


def test_a_show_nobody_follows_is_left_alone(lib):
    """没有订阅（AB 里停用了——档案里还留着旧的 bangumi_id——也没有 subscriptions）：人不追了，不替他订。"""
    _show(lib, ab=False, sidecar={"bangumi_id": 61})
    assert not _proposals(lib)


def test_a_next_season_that_aired_long_ago_is_not_new(lib):
    _show(lib, s2_first_days_ago=400)
    assert not _proposals(lib)


def test_no_next_season_on_tmdb_is_quiet(lib):
    _show(lib, s2_first_days_ago=None)
    assert not lib.diagnose(detectors=[NewSeasonDetector])


def test_an_already_known_next_season_is_not_registered_again(lib):
    _show(lib, sidecar={"subscriptions": {"2": {"source": "autobangumi"}}})
    assert not _proposals(lib)


def test_flattened_numbering_is_left_to_a_human(lib):
    """`season_offsets` 说这部番在库里压平成一季；或者库里第一季的集数比 TMDB 那一季还多（连续编号）：
    TMDB 的"第二季"就是库里第一季的后半段，登记第二季只会把它们再抓一遍。"""
    _show(lib, sidecar={"season_offsets": {"2": 12}})
    assert not _proposals(lib)


def test_a_library_season_longer_than_tmdbs_is_left_to_a_human(lib):
    _show(lib, s1_have=range(1, 25), s1_count=12)
    assert not _proposals(lib)


def test_the_new_season_is_grabbed_in_the_same_run(lib):
    """端到端（迭代）：第一次迭代登记第二季，下一次迭代抓取就按它抓（番组页从标题搜到）。"""
    _show(lib, s2_first_days_ago=10)
    sched = dict(weekly(12, first_days_ago=10))
    lib.mikan(MID2, [MikanItem(title=TPL2.format(n), pub=sched[n]) for n in (1, 2)], search=[SHOW])

    loop = lib.loop(detectors=[NewSeasonDetector, EpisodeAvailableDetector])

    assert [r["args"]["season"] for r in loop.applied("subscribe_season")] == [2]
    assert sorted(r["args"]["episode"] for r in loop.applied("grab_episode")) == [1, 2]
    assert lib.sidecar(SHOW).subscriptions["2"]["source"] == "new-season"
