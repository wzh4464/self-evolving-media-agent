"""订阅着的季抓不了，要说出来；停下来了，要让人收到。

订阅着的季 = sidecar 的 `subscriptions`，或 AB 里落进这一库内季的有效订阅（`abrow.library_season`）。`full` 模式下 AB 照着
RSS 下，抓取出不出力都无所谓；订阅模式下抓取是唯一的下载者。2026-09-27 审查：

- **抓不了却一声不吭**（`EpisodeAvailableDetector` 里两处 bare `continue`）：TMDB 认不出这部番（`create_show_dir` 建的目录搜不到）、
  这一季不是季度番（`is_seasonal`：上百集的连载、一档连播超过 300 天）——订阅着也一样跳过，`grab` 与 `run` 都没有任何发现。
  → 报 `subscription_unserved`；上百集的连载 / 连播的季只看最近播的那几集（AB 的口径：订阅之后发布的），照样抓。
- **停了没人收到**：找不到番组页、候选全被拒、`episode_not_released` 都是 minor——`history.qualifies` 只数 critical /
  important，永远不算卡住、不发信。Re:Zero（AB 9）就停在这个状态。→ 订阅着的季、播出超过 `NO_RELEASE_GRACE_DAYS` 天、
  比库里最新一集还新的缺集（这部番"停下来了"，不是订阅之前的老空缺）报 important。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent import history
from media_agent.plugins.grab import (
    NO_RELEASE_GRACE_DAYS,
    SUBSCRIBED_RECENT_DAYS,
    EpisodeAvailableDetector,
)

SHOW = "新番癸"
TMDB = 3730
MID = "4930"
TPL = "[LoliHouse] 新番癸 / Shinban Ki - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
SEARCH_RSS = "https://mikanani.me/RSS/Search?searchstr=Shinban%20Ki"


def _grabs(findings) -> list[str]:
    return sorted(f.subject for f in findings if f.action and f.action.op == "grab_episode")


def _only(findings, kind):
    [f] = [f for f in findings if f.kind == kind]
    return f


# ------------------------------------------------------------------ 抓不了：说出来
def test_a_subscribed_dir_tmdb_cannot_identify_is_reported(lib):
    """`create_show_dir` / `media-agent subscribe` 建的目录，TMDB 按目录名搜不到：以前抓取一句 `continue`，零发现。"""
    lib.tmdb.add_show(9999, "别的番", seasons={1: weekly(12, first_days_ago=20)})     # TMDB 在线
    sh = lib.show(SHOW)
    sh.sidecar(subscriptions={"1": {"source": "cli", "mikan_id": MID}})

    f = _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "subscription_unserved")

    assert f.severity == "important" and f.subject == "S01" and f.show == SHOW
    assert f.evidence["reason"] == "no_tmdb" and "TMDB" in f.summary


def test_an_ab_only_subscription_without_tmdb_is_minor_in_full_mode(lib):
    """`full` 模式、只有 AB 订阅着：AB 照着 RSS 下，抓不了也不耽误——照样说，minor。订阅模式下就是 important。"""
    lib.tmdb.add_show(9999, "别的番", seasons={1: weekly(12, first_days_ago=20)})
    lib.show(SHOW).season(1).local(f"{SHOW} S01E01.mkv")
    lib.configure(qbit_allow_empty=True)
    lib.bangumi(id=73, official_title=SHOW, title_raw="Shinban Ki", season=1, rss_link=SEARCH_RSS,
                save_path=str(lib.media_root / SHOW / "Season 1"))

    assert _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "subscription_unserved").severity == "minor"
    lib.configure(ab_mode="subscription")
    assert _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "subscription_unserved").severity == "important"


def test_a_subscribed_season_tmdb_does_not_have_is_reported(lib):
    """订阅了第 2 季，TMDB 上没有这一季（404）、也没登记偏移：以前只记一行日志。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: weekly(12, first_days_ago=400)})
    sh = lib.show(SHOW)
    sh.season(1).local(f"{SHOW} S01E01.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, seasons={"1": {"have": [1]}},
               subscriptions={"2": {"source": "autobangumi", "bangumi_id": 74}})

    f = _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "subscription_unserved")

    assert f.subject == "S02" and f.evidence["reason"] == "tmdb_no_season" and f.severity == "important"


def test_a_long_running_subscribed_season_grabs_what_aired_recently(lib):
    """TMDB 这一季 200 集、每周一集（`is_seasonal` 说它是常年连载）：订阅着的只抓最近播的那几集——以前连报都不报。
    没人订的同一部番照旧不碰（补老集是人的事）。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(200, first_days_ago=7 * 199 + 3)                    # 第 200 集三天前
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in (197, 198, 199):
        sh.season(1).local(f"{SHOW} S01E{n:03d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID,
               seasons={"1": {"have": [197, 198, 199]}})
    tpl = "[G] 新番癸 / Shinban Ki - {:03d} [1080p][简日内嵌]"
    lib.mikan(MID, [MikanItem(title=tpl.format(n), pub=dict(sched)[n]) for n in (150, 196, 197, 198, 199, 200)],
              search=[SHOW])

    assert _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector])) == []          # 没人订：照旧

    sh.sidecar(subscriptions={"1": {"source": "cli", "mikan_id": MID}})
    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])
    assert _grabs(fs) == ["S01E200"]
    assert not [f for f in fs if f.kind == "subscription_unserved"]
    assert SUBSCRIBED_RECENT_DAYS >= 7


def test_a_finished_subscribed_season_is_quiet(lib):
    """AB 里还挂着的老订阅（播完一年多，TMDB 说不是季度番、最近也没播过）：没什么可抓，也不报。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: weekly(12, first_days_ago=500)})
    sh = lib.show(SHOW)
    sh.season(1).local(f"{SHOW} S01E01.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, seasons={"1": {"have": [1]}},
               subscriptions={"1": {"source": "autobangumi", "bangumi_id": 75}})

    assert lib.diagnose(detectors=[EpisodeAvailableDetector]) == []


# ------------------------------------------------------------------ 停下来了：让人收到
def _airing_without_page(lib, *, subscribed: bool):
    """第 1、2 集播出一两周了，盘上一集都没有；AB 的订阅是搜索式 RSS（没有番组页 id），Mikan 按名字也搜不到。"""
    lib.configure(qbit_allow_empty=True, ab_mode="subscription")
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: weekly(12, first_days_ago=12)})
    sh = lib.show(SHOW)
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW,
               subscriptions={"1": {"source": "autobangumi", "bangumi_id": 73}} if subscribed else {})
    if not subscribed:
        sh.season(1).local(f"{SHOW} S01E00.mkv")                   # 盘上有这一季（没人订，但抓取看它）
        sh.sidecar(seasons={"1": {"have": []}})
    lib.web.mikan_search(SHOW, [])
    return sh


def test_no_mikan_page_for_a_subscribed_season_is_important(lib):
    _airing_without_page(lib, subscribed=True)

    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.kind == "episode_grabbable"]

    assert "找不到 Mikan 番组页" in f.summary and f.severity == "important"
    assert history.qualifies({"severity": f.severity, "op": None})


def test_an_unreleased_episode_beyond_the_newest_one_on_disk_is_important_when_subscribed(lib):
    """订阅着的季，库里最新是 E03、E04 播出一周多了番组页上一个发布都没有：这部番停了，important。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(12, first_days_ago=30)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in (1, 2, 3):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID,
               seasons={"1": {"have": [1, 2, 3]}}, subscriptions={"1": {"source": "cli", "mikan_id": MID}})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in (1, 2, 3)], search=[SHOW])

    f = _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "episode_not_released")

    assert f.severity == "important" and f.evidence["episodes"] == [4]
    assert f.evidence["grace_days"] == NO_RELEASE_GRACE_DAYS


def test_an_old_gap_before_the_newest_episode_stays_minor(lib):
    """订阅之前就缺着的老集（库里有 E02、E03，缺 E01）：不是"停下来了"，照旧 minor——不为订阅之前的空缺天天报警。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(12, first_days_ago=17)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in (2, 3):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID,
               seasons={"1": {"have": [2, 3]}}, subscriptions={"1": {"source": "cli", "mikan_id": MID}})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in (2, 3)], search=[SHOW])

    f = _only(lib.diagnose(detectors=[EpisodeAvailableDetector]), "episode_not_released")

    assert f.evidence["episodes"] == [1] and f.severity == "minor"


def test_all_candidates_rejected_for_a_stalled_subscribed_episode_is_important(lib):
    """E04 播出一周多，番组页上只有标着别季的同集号发布：订阅着的季停在这里，important；没人订的照旧 minor。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(12, first_days_ago=30)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in (1, 2, 3):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID, seasons={"1": {"have": [1, 2, 3]}})
    other = "[LoliHouse] 新番癸 第二季 / Shinban Ki S2 - 04 [WebRip 1080p][简繁内封字幕]"
    lib.mikan(MID, [MikanItem(title=other, pub=dict(sched)[4])], search=[SHOW])

    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.subject == "S01E04"]
    assert "明显属于别季" in f.summary and f.severity == "minor"

    sh.sidecar(subscriptions={"1": {"source": "cli", "mikan_id": MID}})
    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.subject == "S01E04"]
    assert f.severity == "important"
