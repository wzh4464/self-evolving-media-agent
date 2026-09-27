"""TMDB 把多季压成一季、Mikan 却按季分页：缺的集要到**播出日期对得上的那一页**去找。

**现场**（2026-09-27）：《超超超超超喜欢你的100个女朋友》按 TMDB 的单季 36 集整理成 Season 1 之后，
第 30、31 集（2026-08-09 / 08-16 播出）一直没被抓，而且**一条发现都没有**。抓取为「第 1 季」只解析一个
番组页：按全季播出日期打分选中了 3524（第二季那一页，集号 1–24），而第 30、31 集在第三季的番组页 3997 上，
候选齐全（`[LoliHouse] … Hyakkano - 31 [简繁内封字幕]`）。缺的集在所选页面的 feed 里一个候选都没有时，
检测器直接 `continue`——不抓、不报。

不变式：
- 主页面上某一集没有候选时，去看其它候选番组页里**发布时间覆盖这一集播出日期**的那些页；
- 按日期选页，不拿别季同集号的发布凑数：第一季缺第 6 集，第三季那页上没写季号的 `- 06` 不是候选；
- 仍然找不到、且播出已超过宽限期的，报一条可见的发现（`episode_not_released`，列出查过的番组页），不再静默跳过。

番名与番组页 id、日期均为合成。
"""
from __future__ import annotations

from harness import MikanItem, days_ago

from media_agent.plugins.grab import EpisodeAvailableDetector

SHOW = "百人女友测试番"
C1 = [(n, days_ago(1000 - 7 * (n - 1))) for n in range(1, 13)]         # 第一季
C2 = [(12 + n, days_ago(400 - 7 * (n - 1))) for n in range(1, 13)]     # 第二季 → E13–24
C3 = [(24 + n, days_ago(120 - 7 * (n - 1))) for n in range(1, 13)]     # 第三季 → E25–36
AIR = dict(C1 + C2 + C3)
LOLI = "[LoliHouse] {show} / Hyakkano - {ep:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
LOLI_S2 = "[LoliHouse] {show} 第二季 / Hyakkano 2nd Season - {ep:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _grabs(findings) -> dict[str, dict]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def _not_released(findings, ep: int):
    return [f for f in findings if f.kind == "episode_not_released" and ep in f.evidence["episodes"]]


def _build(lib, *, missing, page1_skip=(), page3_extra=(), page3_skip=()):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(2201, SHOW, seasons={1: C1 + C2 + C3})
    sh = lib.show(SHOW)
    have = [n for n in range(1, 37) if n not in missing]
    for n in have:
        sh.folder("Season 1").local(f"{SHOW} S01E{n:02d}.mkv")
    # 与生产一致：sidecar 里记着的是第二季那一页，按全季日期打分也会选中它
    sh.sidecar(tmdb_id=2201, tmdb_title=SHOW, mikan_id="5002",
               season_offsets={"2": 12, "3": 24}, seasons={"1": {"have": have}})
    p1 = [MikanItem(title=LOLI.format(show=SHOW, ep=n), pub=AIR[n]) for n in range(1, 13) if n not in page1_skip]
    p2 = [MikanItem(title=LOLI_S2.format(show=SHOW, ep=n), pub=AIR[12 + n]) for n in range(1, 13)]
    p3 = [MikanItem(title=LOLI.format(show=SHOW, ep=n), pub=AIR[n]) for n in range(25, 37) if n not in page3_skip]
    p3 += list(page3_extra)
    lib.mikan("5001", p1, search=[SHOW])
    lib.mikan("5002", p2, search=[SHOW])
    lib.mikan("5003", p3, search=[SHOW])
    return sh


def test_missing_episode_is_found_on_the_page_whose_dates_cover_it(lib):
    _build(lib, missing={30})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    g = _grabs(fs)
    assert set(g) == {"S01E30"}
    assert "Hyakkano - 30" in g["S01E30"].action.args["title"]
    assert g["S01E30"].evidence["mikan_id"] == "5003"      # 出处账本记它真正所在的页


def test_a_bare_number_on_another_seasons_page_is_not_a_candidate(lib):
    """第一季缺第 6 集、第一季那页恰好没有它；第三季那页上别的组发了个没写季号的 `- 06`——
    日期对不上第 6 集的播出，不许拿它凑数，只报出来。

    第三季那页在这里是**主番组页**（全季日期打分平手，按最新发布选中它）：没写季号的集号在主页上也要页的发布时间
    覆盖这集的播出日期才算数，不只是兜底查的别的页。"""
    stray = MikanItem(title=f"[SomeRaws] {SHOW} - 06 [1080p][简日内嵌]", pub=AIR[30])
    _build(lib, missing={6}, page1_skip={6}, page3_extra=[stray])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E06" not in _grabs(fs)
    [f] = [f for f in fs if f.subject == "S01E06"]
    assert f.evidence["mikan_id"] == "5003"
    assert any("SomeRaws" in r and "对不上这一集的播出日期" in r for r in f.evidence["rejected_by_season"])


def test_a_bare_number_nowhere_near_any_page_is_reported(lib):
    """缺的集哪一页都没有：兜底查过的页都列出来，报 `episode_not_released`。"""
    _build(lib, missing={6}, page1_skip={6})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E06" not in _grabs(fs)
    [f] = _not_released(fs, 6)
    assert len(f.evidence["mikan_pages"]) > 1


def test_no_candidate_anywhere_is_reported_not_skipped(lib):
    _build(lib, missing={30}, page3_skip={30})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E30" not in _grabs(fs)
    [f] = _not_released(fs, 30)
    assert f.severity == "minor" and "5003" in f.evidence["mikan_pages"]


def test_a_page_whose_releases_start_late_still_covers_its_first_episodes(lib):
    """只有一个番组页、最早的发布是第 1 集播出 20 天后的合集：没写季号的 `- 01` 仍是第 1 集的候选。
    "覆盖"的松弛：页上最早的发布可以比这集的播出晚到 `LATE_SLACK_DAYS`（迟到的搬运组、合集），
    最晚的发布可以比播出早到 `PREAIR_SLACK_DAYS`（抢先党）——不能反过来。"""
    show = "迟到合集测试番"
    air = {n: days_ago(60 - 7 * (n - 1)) for n in range(1, 9)}
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(2202, show, seasons={1: list(air.items())})
    sh = lib.show(show)
    for n in range(4, 9):
        sh.folder("Season 1").local(f"{show} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=2202, tmdb_title=show, mikan_id="5101", seasons={"1": {"have": list(range(4, 9))}})
    batch = days_ago(40)                                   # 第 1 集播出 20 天后
    title = "[LateSub] {show} - {n:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    lib.mikan("5101", [MikanItem(title=title.format(show=show, n=n), pub=batch if n <= 3 else air[n])
                       for n in range(1, 9)], search=[show])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert set(_grabs(fs)) == {"S01E01", "S01E02", "S01E03"}


def test_date_helpers_treat_missing_or_broken_dates_as_unknown():
    """抓取比日期的几处共用 `_day`：缺的、坏的日期一律"不知道"，不抛异常，也不当成能比。"""
    from media_agent.plugins.grab import _plausible_for, _pub_span, _season_fit, _span_covers

    assert _plausible_for({"pub": "2026-13-40"}, "2026-08-09") is None
    assert _plausible_for({"pub": ""}, "2026-08-09") is None
    assert _plausible_for({"pub": "2026-08-10"}, "") is None
    assert _season_fit([{"pub": "2026-08-10"}], ["2026-08-09", "bad"]) == 0.0
    assert _season_fit([{"pub": "2026-08-10"}, {"pub": None}, {"pub": "x"}], ["2026-08-09"]) == 1 / 3
    assert _pub_span([{"pub": "x"}, {}]) is None
    span = _pub_span([{"pub": "2026-08-10"}, {"pub": "bad"}, {"pub": "2026-09-01"}])
    assert span and str(span[0]) == "2026-08-10" and str(span[1]) == "2026-09-01"
    assert _span_covers(span, None) is False and _span_covers(span, "2026-08-09") is True
