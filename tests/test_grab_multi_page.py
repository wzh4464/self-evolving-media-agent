"""TMDB 把多季压成一季、Mikan 却按季分页：缺的集要到**播出日期对得上的那一页**去找。

**现场**（2026-09-27）：《超超超超超喜欢你的100个女朋友》按 TMDB 的单季 36 集整理成 Season 1 之后，
第 30、31 集（2026-08-09 / 08-16 播出）一直没被抓，而且**一条发现都没有**。抓取为「第 1 季」只解析一个
番组页：按全季播出日期打分选中了 3524（第二季那一页，集号 1–24），而第 30、31 集在第三季的番组页 3997 上，
候选齐全（`[LoliHouse] … Hyakkano - 31 [简繁内封字幕]`）。缺的集在所选页面的 feed 里一个候选都没有时，
检测器直接 `continue`——不抓、不报。

不变式：
- 主页面上某一集没有候选时，去看其它候选番组页里**发布时间覆盖这一集播出日期**的那些页；
- 按日期选页，不拿别季同集号的发布凑数：第一季缺第 6 集，第三季那页上没写季号的 `- 06` 不是候选；
- 仍然找不到、且已播出超过 7 天的，报一条可见的发现，不再静默跳过。

番名与番组页 id、日期均为合成。
"""
from __future__ import annotations

import pytest

from harness import MikanItem, days_ago

from media_agent.plugins import grab
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


def _build(lib, *, missing, page1_skip=(), page2_extra=(), page3_extra=(), page3_skip=()):
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
    p2 += list(page2_extra)
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


def test_pinned_page_does_not_grab_from_an_alternate_page(lib):
    sh = _build(lib, missing={30})
    sh.sidecar(pinned=["mikan_id"])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E30" not in _grabs(fs)
    [f] = [f for f in fs if f.subject == "S01E30"]
    assert f.evidence["mikan_pages"] == ["5002"]


def test_wrong_season_on_primary_does_not_block_valid_alternate(lib):
    wrong = MikanItem(title=f"[SomeRaws] {SHOW} 第四季 - 30 [简日内嵌]", pub=AIR[30])
    _build(lib, missing={30}, page2_extra=[wrong])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E30" in _grabs(fs)
    assert "Hyakkano - 30" in _grabs(fs)["S01E30"].action.args["title"]


@pytest.mark.allow("log_failure", match="拉候选番组页")
def test_failed_fallback_pages_count_toward_four_page_limit(lib, monkeypatch):
    _build(lib, missing={30}, page3_skip={30})
    monkeypatch.setattr(grab, "_resolve_mikan_id", lambda *a, **kw: "5002")
    monkeypatch.setattr(grab, "_mikan_candidates", lambda *a, **kw:
                        ["5002", "5001", "5003", "5004", "5005"])
    original = grab._feed_cached
    attempted = []

    def feed(mid, cache):
        attempted.append(mid)
        if mid in {"5001", "5003", "5004"}:
            raise OSError("feed unavailable")
        if mid == "5005":
            raise AssertionError("fifth page must not be fetched")
        return original(mid, cache)

    monkeypatch.setattr(grab, "_feed_cached", feed)
    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert attempted == ["5002", "5001", "5003", "5004"]
    [f] = [f for f in fs if f.subject == "S01E30"]
    assert f.evidence["mikan_pages"] == attempted


def test_alternate_page_is_recorded_as_chosen_release_provenance(lib):
    _build(lib, missing={30})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert _grabs(fs)["S01E30"].evidence["mikan_id"] == "5003"


def test_a_bare_number_on_another_seasons_page_is_not_a_candidate(lib):
    """第一季缺第 6 集、第一季那页恰好没有它；第三季那页上别的组发了个没写季号的 `- 06`——
    日期对不上第 6 集的播出，不许拿它凑数，只报出来。"""
    stray = MikanItem(title=f"[SomeRaws] {SHOW} - 06 [1080p][简日内嵌]", pub=AIR[30])
    _build(lib, missing={6}, page1_skip={6}, page3_extra=[stray])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E06" not in _grabs(fs)
    [f] = [f for f in fs if f.subject == "S01E06"]
    assert "番组页里没有" in f.summary


def test_no_candidate_anywhere_is_reported_not_skipped(lib):
    _build(lib, missing={30}, page3_skip={30})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S01E30" not in _grabs(fs)
    [f] = [f for f in fs if f.subject == "S01E30"]
    assert "番组页里没有" in f.summary and f.severity == "minor"
