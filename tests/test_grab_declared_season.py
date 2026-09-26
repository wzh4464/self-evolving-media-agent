"""抓取不从别季的发布里抓（LAT-03）。

**现场**：2026-09-06 13:51:42 / 43，辉夜大小姐想让我告白的特典位 S00E03、S00E04 被抓进了
`[澄空学园&雪飘工作室&LoliHouse] 辉夜大小姐想让我告白 第三季 / Kaguya-sama wa Kokurasetai S3 - 03 / 04`
——第三季的正片（发布于 2022-04-26 / 05-07）。库里于是多了 `Season 0/辉夜大小姐想让我告白 S00E03.mkv`、`S00E04.mkv`，
sidecar 的 S0 `have` 变成 `[1, 2, 3, 4]`。

原因：抓取按**裸集号**归拢候选（`by_ep[3]`），不看发布自己声明的季号；日期闸只拦"早于播出"（老番重新做种本来就晚），
2022 年的第三季第 3 集对 2021 年播出的特典第 3 集是"合理的"。

不变式：目标是第 N 季，声明第 M 季（M ≠ N）的发布不是候选——除非 sidecar 的 `season_offsets` 把 M 换算进来
（Re:Zero 按 TMDB 压平成一季，`3rd Season - 08` 是 S01E58）；特典位（第 0 季）只从标着特别篇 / OVA / SP 的发布里抓。
被排除的照样报「明显属于别季」，不悄悄跳过。标题用生产上的真名字（辉夜、入间），其余合成。
"""
from __future__ import annotations

from datetime import date, timedelta

from harness import MikanItem, days_ago, weekly

from media_agent.plugins.grab import EpisodeAvailableDetector

KAGUYA = "辉夜大小姐想让我告白"
LOLI_S3 = ("[澄空学园&雪飘工作室&LoliHouse] 辉夜大小姐想让我告白 第三季 / Kaguya-sama wa Kokurasetai S3 - {:02d} "
           "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")


def _grabs(findings) -> dict[str, dict]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def _kaguya(lib, extra_items=()):
    """特典 E1–E4 在 2021 年前后播出、E5 是近期的特典（让 S0 算"在播"）；库里只有 S0 的 1、2。"""
    lib.configure(qbit_allow_empty=True)
    specials = [(1, "2019-05-01"), (2, "2020-06-01"), (3, "2021-05-19"), (4, "2021-05-26"),
                (5, days_ago(20))]
    lib.tmdb.add_show(83121, KAGUYA, seasons={0: specials, 1: weekly(12, first_days_ago=2700),
                                               2: weekly(12, first_days_ago=2300),
                                               3: weekly(13, first_days_ago=1650)})
    sh = lib.show(KAGUYA)
    for n in (1, 2):
        sh.folder("Season 0").local(f"{KAGUYA} S00E{n:02d}.mkv")
    sh.sidecar(tmdb_id=83121, tmdb_title=KAGUYA, mikan_id="2699", seasons={"0": {"have": [1, 2]}})
    items = [MikanItem(title=LOLI_S3.format(3), pub="2022-04-26"),
             MikanItem(title=LOLI_S3.format(4), pub="2022-05-07"), *extra_items]
    lib.mikan("2699", items, search=[KAGUYA])
    return sh


def test_lat03_a_third_season_release_is_not_grabbed_into_a_special_slot(lib):
    _kaguya(lib)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert not _grabs(fs)
    off = {f.subject: f for f in fs if "明显属于别季" in f.summary}
    assert set(off) == {"S00E03", "S00E04"}
    assert any("S3 - 03" in t for t in off["S00E03"].evidence["rejected_by_season"])


def test_a_bare_numbered_release_is_not_grabbed_into_a_special_slot(lib):
    """同一页上别的组不写季号（`Ultra Romantic - 03` 就是第三季的副标题）：特典位只收标着特典的。"""
    _kaguya(lib, [MikanItem(title="[Nekomoe kissaten] Kaguya-sama wa Kokurasetai - Ultra Romantic - 03 [1080p][简日内嵌]",
                            pub="2022-04-27")])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert "S00E03" not in _grabs(fs)
    [f] = [f for f in fs if f.subject == "S00E03"]
    assert "明显属于别季" in f.summary


def test_a_release_marked_as_a_special_is_still_grabbed(lib):
    """标着特典的照抓——带着季号的也算（`第三季 OVA`：那是挂在第三季名下的特典）。"""
    _kaguya(lib, [MikanItem(title="[LoliHouse] 辉夜大小姐想让我告白 第三季 / Kaguya-sama S3 OVA - 05 "
                                  "[WebRip 1080p][简繁内封字幕]", pub=days_ago(19))])

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S00E05"}
    assert g["S00E05"].action.args["season"] == 0


# ------------------------------------------------------------------ 普通季：声明的季号要对得上
IRUMA = "入间同学入魔了！"


def _iruma(lib, titles: list[str], offsets: dict | None = None):
    """桜都把 TMDB 的第四季标成「第3季 / 3rd Season」（生产 2026-09 的真名字）；Nix-Raws 写 S04E20。"""
    lib.configure(qbit_allow_empty=True)
    schedule = weekly(21, first_days_ago=140)
    lib.tmdb.add_show(91801, IRUMA, seasons={4: schedule})
    sh = lib.show(IRUMA)
    for n in range(1, 20):
        sh.season(4).local(f"{IRUMA} S04E{n:02d}.mkv")
    sh.sidecar(tmdb_id=91801, tmdb_title=IRUMA, mikan_id="3918",
               season_offsets=offsets or {}, seasons={"4": {"have": list(range(1, 20))}})
    pub = dict(schedule)
    lib.mikan("3918", [MikanItem(title=t.format(ep=e), pub=pub[e]) for t in titles for e in (20, 21)],
              search=[IRUMA])
    return sh


SAKURATO = "[桜都字幕组] 入间同学入魔了！第3季 / Mairimashita! Iruma-kun 3rd Season [{ep}][1080p][简繁内封]"
NIX = ("[Nix-Raws] 入间同学入魔了！第四季 / 魔入りました！入间くん 第4シリーズ / Mairimashita Iruma-kun S04E{ep} "
       "[CR WEB-DL 1080p AVC AAC][简繁内封]")


def test_a_release_declaring_another_season_is_not_a_candidate(lib):
    _iruma(lib, [SAKURATO, NIX])

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S04E20", "S04E21"}
    assert all("Nix-Raws" in f.action.args["title"] for f in g.values())
    assert g["S04E20"].evidence["rejected_by_season"]


def test_only_other_season_releases_are_reported_not_skipped_silently(lib):
    _iruma(lib, [SAKURATO])

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert not _grabs(fs)
    assert {f.subject for f in fs if "明显属于别季" in f.summary} == {"S04E20", "S04E21"}


def test_season_offsets_map_a_declared_season_in(lib):
    """人在 sidecar 里登记了换算（`{"3": 0}`：桜都的「第3季」就是这里的第 4 季，集号不变）：照常是候选。"""
    _iruma(lib, [SAKURATO], offsets={"3": 0})

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S04E20", "S04E21"}


def test_flattened_numbering_only_lands_on_the_mapped_episode(lib):
    """Re:Zero 按 TMDB 压平成一季（`{"3": 50}`）：`3rd Season - 08` 是 S01E58，不是 S01E08——以前两处都登记。"""
    lib.configure(qbit_allow_empty=True)
    start = date.today() - timedelta(days=60)
    eps = [(n, (date(2016, 4, 1) + timedelta(days=7 * n)).isoformat()) for n in range(1, 51)]
    eps += [(50 + i, (start + timedelta(days=7 * (i - 1))).isoformat()) for i in range(1, 10)]
    lib.tmdb.add_show(65942, "Re:从零开始的异世界生活", seasons={1: eps})
    sh = lib.show("Re:从零开始的异世界生活")
    have = [n for n in range(1, 58) if n != 8]                  # 第 8 集也缺着
    for n in have:
        sh.season(1).local(f"Re:从零开始的异世界生活 S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=65942, tmdb_title="Re:从零开始的异世界生活", mikan_id="3300",
               season_offsets={"3": 50}, seasons={"1": {"have": have}})
    lib.mikan("3300", [MikanItem(title="[Fyy Raws] Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][简繁内封]",
                                 pub=dict(eps)[58])], search=["Re:从零开始的异世界生活"])

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S01E58"}
