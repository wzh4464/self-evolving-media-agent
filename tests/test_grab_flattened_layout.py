"""TMDB 压平成一季、库里分成几季：登记了集号偏移的库内季照样抓（按 TMDB 那一季的一段算缺哪几集）。

**现场**（生产 2026-09-27 回放）：TMDB 把《超超超超超喜欢你的100个女朋友》（223564）列成**一季 36 集**，库里是 Season 1、2、3。
抓取的"季的编排"闸（`season_layout_mismatch`）看到库里有 TMDB 没有的 Season 2、3，整部番一集都不抓——在任何集号偏移换算
之前。就算没有这道闸，`season_episodes(223564, 3)` 也是空的。AB 37（第三季，`episode_offset -24`）是今天唯一抓它的；订阅模式下
S03E06、E07 永远缺着。集号偏移的换算（`- 25` → S03E01）只在 TMDB 另有第三季的测试现场里跑过。而这条发现的建议是"在
season_offsets 里登记"——那改变不了"库里多出来的季"。

《胆大党》同一类：TMDB 第 1 季 24 集，库里 Season 1、2。

不变式：
- 库内第 L 季 TMDB 没有、而它登记了负的集号偏移 `-B`（sidecar 的 `episode_offsets`，没登记时是 AB 落进这一季的订阅行上的）：
  它是 TMDB 上 L 之前最后一季（压平的那一季）从第 B+1 集起的一段，一直到下一个这样登记了的库内季。缺哪几集、播出日期、
  在不在播都按这一段算；发布按 TMDB 那一季的编号认（`S01E30` 是库内 S03E06）。
- 登记了的季照样抓；还有没登记的多出来的季时，只停没法确定范围的那几季（没登记的、以及 TMDB 那一季本身），报
  `season_layout_mismatch`，建议写的是 `episode_offsets`（按库内各季的集数推测一个值，核对后再写）。
- TMDB 的季列表（扫描按条目缓存 30 天）说没有、可分集表（6 小时）有的季不算多出来的——新一季刚上 TMDB。
"""
from __future__ import annotations

from datetime import date, timedelta

from harness import MikanItem, weekly

from media_agent.plugins.grab import EpisodeAvailableDetector

HYAKKANO = "超超超超超喜欢你的100个女朋友"
TMDB = 2235_64
MID = "3417"
NIX = ("[Nix-Raws] Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo S01E{:02d} "
       "[CR WEB-DL 1080p AVC AAC][简繁内封]")


def _three_cours() -> list[tuple[int, str]]:
    """一季 36 集：第 1–12 集约三年前、13–24 集一年半前、25–36 集这一档（周播，最后一集 13 天前）。"""
    out = []
    for cour, days in enumerate((1000, 600, 90)):
        start = date.today() - timedelta(days=days)
        out += [(cour * 12 + i + 1, (start + timedelta(days=7 * i)).isoformat()) for i in range(12)]
    return out


def _hyakkano(lib, *, offsets=None, ab_offset=None, s3=(1, 2, 3, 4, 5, 8, 9, 10, 11, 12)):
    lib.configure(qbit_allow_empty=True)
    sched = _three_cours()
    air = dict(sched)
    lib.tmdb.add_show(TMDB, HYAKKANO, seasons={1: sched})
    sh = lib.show(HYAKKANO)
    have = {1: range(1, 13), 2: range(1, 13), 3: s3}
    for sn, eps in have.items():
        for n in eps:
            sh.season(sn).local(f"{HYAKKANO} S{sn:02d}E{n:02d}.mkv")
    fields = dict(tmdb_id=TMDB, tmdb_source="human", tmdb_title=HYAKKANO, mikan_id=MID,
                  seasons={str(sn): {"have": list(eps)} for sn, eps in have.items()})
    if offsets is not None:
        fields["episode_offsets"] = offsets
    sh.sidecar(**fields)
    if ab_offset is not None:
        sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=ab_offset,
                   rss_link="https://mikanani.me/RSS/Search?searchstr=100-nin%20no%20Kanojo")
    lib.mikan(MID, [MikanItem(title=NIX.format(n), pub=air[n]) for n in range(25, 37)], search=[HYAKKANO])
    return sh


def _grabs(findings) -> dict[str, object]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def test_ab_37s_offset_maps_season_3_onto_the_flattened_tmdb_season(lib):
    """AB 37 的偏移（还没迁进 sidecar 时就是 AB 行上的）：库内 S3 = TMDB S1 E25–E36。缺的 S03E06、E07 是 TMDB 的 E30、E31，
    按 AB 37 自己那组的 `S01E30`、`S01E31` 抓。Season 2 没登记偏移：它与 Season 1 在 TMDB 上各是哪一段说不清，这两季不抓、
    报出来，建议写 episode_offsets。"""
    _hyakkano(lib, ab_offset=-24)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    g = _grabs(fs)
    assert set(g) == {"S03E06", "S03E07"}
    assert g["S03E06"].action.args["season"] == 3 and g["S03E06"].action.args["episode"] == 6
    assert "S01E30" in g["S03E06"].action.args["title"] and "S01E31" in g["S03E07"].action.args["title"]
    [m] = [f for f in fs if f.kind == "season_layout_mismatch"]
    assert m.evidence["extra"] == [2] and m.evidence["mapped"] == {"3": [1, 24]}
    assert m.evidence["suggested_offsets"] == {"2": -12}
    assert "episode_offsets" in m.summary and "season_offsets" not in m.summary


def test_with_every_extra_season_registered_nothing_is_blocked(lib):
    """Season 2 也登记了（-12）：三季都落得进 TMDB 第 1 季的一段——Season 1 是 E1–E12、不会把 E13 起的当成缺。"""
    _hyakkano(lib, offsets={"2": -12, "3": -24})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert set(_grabs(fs)) == {"S03E06", "S03E07"}
    assert not [f for f in fs if f.kind in ("season_layout_mismatch", "episode_not_released",
                                            "episode_numbering_mismatch")]


def test_without_offsets_the_advice_is_episode_offsets_with_a_guess(lib):
    """《胆大党》的形态：TMDB 一季 24 集、库里 Season 1、2，都没登记。不抓（以免把 S02 的集当成 S01 的 E13–E24 再下一遍），
    建议写的是 episode_offsets（按 Season 1 的 12 集推测 -12），不是改变不了什么的 season_offsets。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(24, first_days_ago=200)
    lib.tmdb.add_show(2404_11, "胆大党", seasons={1: sched})
    sh = lib.show("胆大党")
    for sn in (1, 2):
        for n in range(1, 13):
            sh.season(sn).local(f"胆大党 S{sn:02d}E{n:02d}.mkv")
    sh.sidecar(tmdb_id=2404_11, tmdb_source="human", tmdb_title="胆大党",
               seasons={"1": {"have": list(range(1, 13))}, "2": {"have": list(range(1, 13))}})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert not _grabs(fs)
    [m] = fs
    assert m.kind == "season_layout_mismatch" and m.evidence["suggested_offsets"] == {"2": -12}
    assert '"2": -12' in m.summary and "season_offsets" not in m.summary


def test_a_season_the_cached_show_list_does_not_know_yet_is_not_extra(lib):
    """TMDB 的季列表按条目缓存 30 天（扫描的 `tmdbshow:<id>`）：缓存之后 TMDB 才加的第 2 季，分集表（6 小时）里有。以前抓到
    第二季的头几集、sidecar-sync 记下 `seasons["2"]` 之后，整部番因为"库内有 Season [2] 而 TMDB 只有 Season [1]"停抓，
    直到缓存过期（2026-09-27 审查复现；生产上《冰之城墙》缓存的列表已经提前有了第二季）。"""
    lib.configure(qbit_allow_empty=True)
    s1 = weekly(12, first_days_ago=400)
    lib.tmdb.add_show(3801, "旧番庚", seasons={1: s1})
    sh = lib.show("旧番庚")
    for n in range(1, 13):
        sh.season(1).local(f"旧番庚 S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=3801, tmdb_source="human", tmdb_title="旧番庚", seasons={"1": {"have": list(range(1, 13))}})
    lib.scan()                                                    # 缓存条目：此刻只有第 1 季
    s2 = weekly(12, first_days_ago=30)
    lib.tmdb.add_show(3801, "旧番庚", seasons={1: s1, 2: s2})       # TMDB 之后加了第 2 季
    for n in (1, 2):
        sh.season(2).local(f"旧番庚 S02E{n:02d}.mkv")
    sh.sidecar(seasons={"1": {"have": list(range(1, 13))}, "2": {"have": [1, 2]}},
               subscriptions={"2": {"source": "new-season", "mikan_id": "5001"}})
    tpl = "[LoliHouse] 旧番庚 第二季 / Jiufan Geng S2 - {:02d} [WebRip 1080p][简繁内封字幕]"
    lib.mikan("5001", [MikanItem(title=tpl.format(n), pub=dict(s2)[n]) for n in range(1, 5)], search=["旧番庚"])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert set(_grabs(fs)) == {"S02E03", "S02E04"}
    assert not [f for f in fs if f.kind == "season_layout_mismatch"]


def test_a_subscribed_season_beyond_the_flattened_season_says_which_part_is_missing(lib):
    """AB 里订了下一季（第 4 季，偏移 -36），TMDB 那一季还只到第 36 集：没有可抓的，说清楚缺的是"第 1 季第 37 集起"这一段，
    而不是含糊地说"第 1 季还没有分集表"（它有 36 集）。"""
    _hyakkano(lib, offsets={"2": -12, "3": -24, "4": -36}, s3=range(1, 13))
    lib.show(HYAKKANO).sidecar(subscriptions={"4": {"source": "autobangumi", "bangumi_id": 41}})

    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.kind == "subscription_unserved"]

    assert f.subject == "S04" and f.evidence["reason"] == "tmdb_no_episodes"
    assert "第 37 集起" in f.summary
