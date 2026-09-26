"""sidecar 的 `subscriptions`：盘上一集都还没有的季照样抓（第 5 阶段 T2 的底座）。

以前抓取只认 sidecar `seasons` 里的季（`grab.py`：`if not sc.seasons: continue`、只迭代已有的季键），而 `seasons` 是
sidecar-sync 按**盘上的文件**记的；扫描又只登记有文件的目录（`scan.build_state`）。于是新番、老番的新一季，在别的什么
（今天是 AutoBangumi）放进第一个文件之前，永远不会被抓——AB 一退役，订阅就停了（critic §4）。

`subscriptions`（人的意图）：`{"1": {"source": ..., "mikan_id": ...}}` = 要抓的库内季。扫描登记"只有订阅档案"的目录，
抓取把订阅的季与盘上的季一起看，订阅里的番组页 id 先试。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent.plugins.grab import EpisodeAvailableDetector

SHOW = "新番甲"
TMDB = 3301
MID = "4501"
TPL = "[LoliHouse] 新番甲 / Shinban Kou - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _grabs(findings) -> dict[str, object]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def _subscribed(lib, *, subscriptions, seasons=None, first_days_ago=20, eps=(1, 2, 3),
                release_season=1, **fields):
    """一个只有订阅档案的番目录（或加上 `seasons` 描述的盘上文件）；第 `release_season` 季周播 12 集。"""
    sched = weekly(12, first_days_ago=first_days_ago)
    lib.tmdb.add_show(TMDB, SHOW, seasons={release_season: sched, **(seasons or {})})
    sh = lib.show(SHOW)
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, subscriptions=subscriptions, **fields)
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in eps], search=[SHOW])
    return sh


def test_a_dir_holding_only_a_subscription_is_scanned(lib):
    _subscribed(lib, subscriptions={"1": {"source": "cli", "mikan_id": MID}})

    state = lib.scan(resolve_tmdb=True)

    [show] = state.shows
    assert show.dir_name == SHOW and show.files == [] and not show.is_movie
    assert show.tmdb_id == TMDB


def test_a_dir_with_only_a_plain_sidecar_is_still_ignored(lib):
    """没有订阅的档案（历史遗留的空目录）照旧不登记：不因为多了这一步去抓用户删掉的番。"""
    sh = lib.show(SHOW)
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", seasons={"1": {"have": [1, 2]}})

    assert lib.scan(resolve_tmdb=False).shows == []


def test_a_corrupt_sidecar_does_not_register_an_empty_dir(lib):
    sh = lib.show(SHOW)
    (sh.path / ".media-agent.json").write_text('{"subscriptions": {"1": ', encoding="utf-8")

    assert lib.scan(resolve_tmdb=False).shows == []


def test_grab_starts_a_subscribed_season_with_nothing_on_disk(lib):
    _subscribed(lib, subscriptions={"1": {"source": "cli", "mikan_id": MID}})

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S01E01", "S01E02", "S01E03"}
    assert g["S01E01"].action.args["show_dir"] == str(lib.path(SHOW))
    assert g["S01E01"].evidence["mikan_id"] == MID


def test_a_subscribed_season_is_grabbed_next_to_the_seasons_on_disk(lib):
    """第一季早就收齐在盘上（不在播，抓取不看它）；第二季订阅了、盘上一集都没有。"""
    lib.configure(qbit_allow_empty=True)
    s1 = weekly(12, first_days_ago=500)
    sh = _subscribed(lib, subscriptions={"2": {"source": "new-season", "mikan_id": MID}},
                     seasons={1: s1}, release_season=2)
    for n in range(1, 13):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S02E01", "S02E02", "S02E03"}
    assert all(f.action.args["season"] == 2 for f in g.values())


def test_the_subscription_page_is_tried_before_searching(lib):
    """订阅里记着番组页（AB 订阅的 bangumiId、`subscribe --mikan`）：它排第一个候选；搜索搜到的别的页对不上日期。"""
    _subscribed(lib, subscriptions={"1": {"source": "autobangumi", "mikan_id": MID}})
    lib.web.mikan_search(SHOW, ["9999"])            # 覆盖：按名字搜到的是另一页
    lib.web.mikan_feed("9999", [MikanItem(title=TPL.format(1), pub="2019-01-01")])

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S01E01", "S01E02", "S01E03"}
    assert {f.evidence["mikan_id"] for f in g.values()} == {MID}


def test_the_first_grab_lands_in_the_subscribed_season(lib):
    """端到端（像 `run` 那样迭代）：种子加进 `<番目录>/Season 1`，sidecar 的 have 记上。"""
    _subscribed(lib, subscriptions={"1": {"source": "cli", "mikan_id": MID}}, eps=(1,), first_days_ago=3)

    loop = lib.loop(detectors=[EpisodeAvailableDetector])

    [rec] = loop.applied("grab_episode")
    assert rec["save_path"] == str(lib.path(SHOW) / "Season 1")
    assert lib.sidecar(SHOW).seasons["1"]["have"] == [1]
