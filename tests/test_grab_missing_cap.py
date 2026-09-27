"""一部番一次最多抓 `MAX_PER_SHOW` 集——但只在"番组页上真有可抓的"那些集里数，不在全部缺的集里数。

**现场**（生产 AB 6《正相反的你与我》，2026-09-27 回放）：TMDB 把两档压平成第 1 季 25 集，AB 订阅的是「第二季」的页 4037。
作为切换之后的新订阅：AB 订阅那一刻把页上已发布的 E13–E23 补进来，第一档的 E1–E12 不在这个页上。以前 `missing[:6]`
先截断、再找候选：每 30 分钟都只看缺的前 6 集（E1–E6，页上没有），还在播的 E24、E25 永远轮不到——唯一的报告是一条 minor 的
`episode_not_released [1..6]`。库里的【我推的孩子】第 1 季缺 [12..35]，也是永远只看 [12..17]。

不变式：先给每一集找候选，再在有候选的集里按集号从小到大取前 `MAX_PER_SHOW` 集；页上什么都没有的集照旧统一报。
"""
from __future__ import annotations

from datetime import date, timedelta

from harness import MikanItem

from media_agent.plugins.grab import MAX_PER_SHOW, EpisodeAvailableDetector

SHOW = "正相反的你与我"
TMDB = 3912
MID = "4037"
TPL = "[SweetSub] 相反的你和我 / Seihantai na Kimi to Boku - {:02d} [WebRip 1080p][简日内嵌]"


def _two_cours() -> list[tuple[int, str]]:
    """第 1–12 集八个月前、第 13–25 集这一档（周播，E25 四天前）。"""
    out = []
    for cour, (days, n) in enumerate(((240, 12), (88, 13))):
        start = date.today() - timedelta(days=days)
        out += [(cour * 12 + i + 1, (start + timedelta(days=7 * i)).isoformat()) for i in range(n)]
    return out


def _mid_season(lib, *, have=range(13, 24)):
    lib.configure(qbit_allow_empty=True)
    sched = _two_cours()
    air = dict(sched)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in have:
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mp4")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, seasons={"1": {"have": list(have)}},
               subscriptions={"1": {"source": "autobangumi", "bangumi_id": 6, "mikan_id": MID}})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=air[n]) for n in range(13, 26)], search=[SHOW])


def _grabs(findings) -> list[int]:
    return sorted(f.action.args["episode"] for f in findings if f.action and f.action.op == "grab_episode")


def test_episodes_the_page_has_are_grabbed_even_behind_older_gaps(lib):
    _mid_season(lib)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert _grabs(fs) == [24, 25]
    [f] = [f for f in fs if f.kind == "episode_not_released"]
    assert f.evidence["episodes"] == list(range(1, 13))           # 第一档不在这个页上：照旧统一报


def test_the_cap_counts_only_episodes_with_a_candidate(lib):
    """这一档一集都没有：页上 13 集全可抓，一次只抓前 `MAX_PER_SHOW` 集（按集号从小到大）。"""
    _mid_season(lib, have=())

    assert _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector])) == list(range(13, 13 + MAX_PER_SHOW))


def test_an_unsubscribed_season_still_only_looks_at_the_first_gaps(lib):
    """没人订的季（没有 sidecar 订阅、没有 AB 订阅）：照旧只看缺的前 `MAX_PER_SHOW` 集。回放里的【我推的孩子】库里只有
    第一档 11 集、TMDB 压平成一季 35 集：放开之后要越过第二档整档的空缺去抓第三档——补不补老档是人的决定。"""
    _mid_season(lib)
    lib.show(SHOW).sidecar(subscriptions={})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert _grabs(fs) == []
    [f] = [f for f in fs if f.kind == "episode_not_released"]
    assert f.evidence["episodes"] == list(range(1, 7))


def test_an_ab_subscription_for_the_season_counts_as_subscribed(lib):
    """AB 里落进这一季的有效订阅（盘上已有这一季、sidecar 没写订阅——生产 35 条订阅的常态）同样算订阅着。"""
    _mid_season(lib)
    lib.show(SHOW).sidecar(subscriptions={})
    lib.show(SHOW).bangumi(6, title_raw="Seihantai na Kimi to Boku", season=1,
                           rss_link=f"https://mikanani.me/RSS/Bangumi?bangumiId={MID}&subgroupid=583")

    assert _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector])) == [24, 25]
