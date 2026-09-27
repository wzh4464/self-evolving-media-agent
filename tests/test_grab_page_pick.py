"""抓取选番组页：订阅指定的页（AB 订阅行的 bangumiId、sidecar 订阅的 `mikan_id`）优先；按播出日期打分平手时，选离缺的集最近的页。

**现场**（生产 AB 订阅 id 9，2026-09-27 回放）：TMDB 把《Re:从零开始的异世界生活》压平成一季、2016 → 2026 共 85 集，库里也按
这一季连续编号。候选页有两个：sidecar 里人很久以前填的 2259（Mikan 的「第二季」页，2020–2021 年的发布）与 AB 订阅的 3951
（「第四季」页，正在播的这一档）。`_season_fit` 按这一季**全部**已播集的播出日期开窗——十年的窗口把每个 Re:Zero 的页都框进去，
两页都是 1.0；以前严格的 `>` 让排在前面的 2259（上一轮记住的 + sidecar 存的）赢，缓存成 `mikanpick`，从此只看一个没有这一档
发布的页。E83、E84 在 3951 上早就有了，一直没抓；唯一的信号是一条 minor 的 `episode_not_released`，说的还是 2259。
AB 在的时候 AB 自己按 3951 下；订阅模式下（AB 不下）这部番就停了。

不变式：
- 这一季有订阅指定的页：订阅的 `mikan_id`，以及 AB 里对这一库内季的每条有效订阅的 bangumiId（与集号偏移同一个口径：AB 行
  只对它落进的那一季）。它们在播出日期打分平手时优先。
- 平手时再比"离缺的集近不近"：按缺的那几集的播出日期开窗打分；再平手比最新一条发布的日期；最后才是候选的先后。
标题用生产上的名字形态，id、日期合成。
"""
from __future__ import annotations

from datetime import date, timedelta

from harness import MikanItem

from media_agent.plugins.grab import EpisodeAvailableDetector

SHOW = "Re:从零开始的异世界生活"
TMDB = 3951_0
OLD, NEW = "2259", "3951"
TPL = "[Nix-Raws] Re:Zero kara Hajimeru Isekai Seikatsu - {:02d} [CR WEB-DL 1080p][简繁内封]"


def _flat_season() -> list[tuple[int, str]]:
    """三档压平成一季：第 1–12 集十年前、13–24 集六年前、25–36 集这一档（周播，最后一集 13 天前）。"""
    out = []
    for cour, days in enumerate((3650, 2200, 90)):
        start = date.today() - timedelta(days=days)
        out += [(cour * 12 + i + 1, (start + timedelta(days=7 * i)).isoformat()) for i in range(12)]
    return out


def _rezero(lib, *, ab_page: str | None = NEW, sources: bool = True, have_to: int = 34, subscription=None):
    lib.configure(qbit_allow_empty=True)
    sched = _flat_season()
    air = dict(sched)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in range(1, have_to + 1):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    fields = dict(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=OLD,
                  seasons={"1": {"have": list(range(1, have_to + 1))}})
    if sources:
        fields["sources"] = [{"group": "Nix-Raws",
                              "rss_link": f"https://mikanani.me/RSS/Bangumi?bangumiId={NEW}&subgroupid=554"}]
    if subscription:
        fields["subscriptions"] = {"1": subscription}
    sh.sidecar(**fields)
    if ab_page:
        sh.bangumi(9, title_raw="Re Zero", season=1,
                   rss_link=f"https://mikanani.me/RSS/Bangumi?bangumiId={ab_page}&subgroupid=554")
    lib.mikan(OLD, [MikanItem(title=TPL.format(n), pub=air[n]) for n in range(13, 25)], search=[SHOW])
    lib.mikan(NEW, [MikanItem(title=TPL.format(n), pub=air[n]) for n in range(25, 37)])
    return sh


def _grabs(findings) -> dict[str, object]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def test_the_ab_subscription_page_wins_a_tie_on_a_flattened_season(lib):
    _rezero(lib)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    g = _grabs(fs)
    assert set(g) == {"S01E35", "S01E36"}
    assert {f.evidence["mikan_id"] for f in g.values()} == {NEW}
    assert not [f for f in fs if f.kind == "episode_not_released"]


def test_without_an_ab_row_the_page_near_the_missing_episodes_wins_the_tie(lib):
    """AB 那一行没了（停用、或从没在 AB 订过），3951 只在 sidecar 的 sources 里：两页平手，离缺的那几集近的赢——
    排在前面的 2259 在这几集的播出日期附近一条发布都没有。"""
    _rezero(lib, ab_page=None)

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S01E35", "S01E36"}
    assert {f.evidence["mikan_id"] for f in g.values()} == {NEW}


def test_a_remembered_wrong_pick_is_not_sticky(lib):
    """上一轮已经选中、记进缓存的旧页（生产上 `mikanpick:65942:1 = 2259`）：下一轮照样重新打分，平手时不再赖着不走。"""
    _rezero(lib, ab_page=None, sources=False)
    first = lib.diagnose(detectors=[EpisodeAvailableDetector])
    assert not _grabs(first)                                     # 只知道 2259：没有这一档的发布
    sc = lib.sidecar(SHOW)
    sc.sources = [{"group": "Nix-Raws", "rss_link": f"https://mikanani.me/RSS/Bangumi?bangumiId={NEW}&subgroupid=1"}]
    lib.show(SHOW).sidecar(sources=sc.sources)

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert {f.evidence["mikan_id"] for f in g.values()} == {NEW}


def test_the_subscription_page_wins_a_tie_against_an_earlier_candidate(lib):
    """两页的发布一模一样（同一档、Mikan 上重复建的页）：订阅指定的那一页赢，不管它在候选里排第几。"""
    lib.configure(qbit_allow_empty=True)
    sched = [(n, (date.today() - timedelta(days=40 - 7 * (n - 1))).isoformat()) for n in range(1, 6)]
    lib.tmdb.add_show(3902, "订阅甲", seasons={1: sched})
    sh = lib.show("订阅甲")
    sh.sidecar(tmdb_id=3902, tmdb_source="human", tmdb_title="订阅甲", mikan_id="7001",
               subscriptions={"1": {"source": "cli", "mikan_id": "7002"}})
    items = lambda: [MikanItem(title=f"[G] Dingyue Jia - {n:02d} [1080p][简日内嵌]", pub=d) for n, d in sched]  # noqa: E731
    lib.mikan("7001", items(), search=["订阅甲"])
    lib.mikan("7002", items())

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {f"S01E{n:02d}" for n in range(1, 6)}
    assert {f.evidence["mikan_id"] for f in g.values()} == {"7002"}


def test_an_ab_row_for_another_season_does_not_steer_this_one(lib):
    """AB 行只对它落进的那一季（`abrow.library_season`）：第 2 季的订阅页不当第 1 季的候选首选。"""
    _rezero(lib, ab_page=None)
    lib.show(SHOW).bangumi(10, title_raw="Re Zero S2", season=2,
                           rss_link=f"https://mikanani.me/RSS/Bangumi?bangumiId={OLD}&subgroupid=1")

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert {f.evidence["mikan_id"] for f in g.values()} == {NEW}


def test_a_page_the_subscription_names_beats_a_nearer_one(lib):
    """人（或 AB 的订阅）为这一季指定了页：播出日期打分平手时它赢，哪怕别的页离缺的集更近——订阅指定的页是人的意图，
    "近不近"只是猜；它真停了由 `episode_not_released` 报出来（订阅着的季停下来是 important）。"""
    _rezero(lib, ab_page=None, subscription={"source": "cli", "mikan_id": OLD})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    [f] = [f for f in fs if f.kind == "episode_not_released"]
    assert f.evidence["mikan_id"] == OLD and f.severity == "important"
    assert not [f for f in fs if f.action]
