"""真检测器产出的集位级 / 季级发现，指纹按 `subject` 认（`history.target_of` = `show#subject`）。

为什么（2026-09-26 复审，变异测试 H1k2–H1k7）：`Finding.subject` 让卡住检测、`ack` 与通知去重按"这一集 / 这一季"
认同一个问题。只有 seal_conflict 与 suspicious_episode_parse 有测试——其余六处删掉 `subject=` 全套照绿。而可抓取、
缺集、找不到番组页这几种**没有 path 也没有种子**：没有 subject，`target_of` 退回番名，一部番的每一集（每一季）
共用一个指纹——一个 `ack` 就把整部番的都静音了，不同集连起来的"连续段"也会被当成同一个卡住的问题。
有 path 的（phantom_only、pending_ownership）path 是桶里第一个文件，谁排第一会变。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent import history
from media_agent.plugins.builtin import CategoryConsolidationDetector, DuplicateEpisodeDetector
from media_agent.plugins.grab import EpisodeAvailableDetector
from media_agent.plugins.subscription import IncompleteSeasonDetector

GB = 600_000_000
SHOW = "尼古喵喵"


def _title(ep: int, group: str = "LoliHouse") -> str:
    return f"[{group}] 尼古喵喵 / Yani Neko - {ep:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _grab_scene(lib, extra_candidates: int = 0):
    """S01 已有 1–7，已播 9 集：8、9 两集可抓。"""
    lib.tmdb.enabled = True
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 8):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", seasons={"1": {"have": list(range(1, 8))}})
    items = [MikanItem(title=_title(n), pub=dict(schedule)[n]) for n in (8, 9)]
    items += [MikanItem(title=_title(9, f"G{i}"), pub=dict(schedule)[9])
              for i in range(extra_candidates)]
    lib.mikan("3500", items, search=[SHOW])
    return sh


def _grabbable(lib) -> dict[str, object]:
    fs = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector])
          if f.action and f.action.op == "grab_episode"]
    return {f.subject: f for f in fs}


def test_two_grabbable_episodes_of_one_show_have_distinct_fingerprints(lib):
    _grab_scene(lib)
    g = _grabbable(lib)
    assert set(g) == {"S01E08", "S01E09"}
    assert history.target_of(g["S01E09"]) == f"{SHOW}#S01E09"
    assert history.fingerprint(g["S01E08"]) != history.fingerprint(g["S01E09"])


def test_grabbable_fingerprint_ignores_the_candidate_count(lib):
    _grab_scene(lib)
    before = _grabbable(lib)["S01E09"]
    schedule = dict(weekly(12, first_days_ago=60))
    lib.mikan("3500", [MikanItem(title=_title(n), pub=schedule[n]) for n in (8, 9)]
              + [MikanItem(title=_title(9, "G2"), pub=schedule[9])])
    lib.cfg.cache_db.unlink()                                     # feed 有缓存：让第二轮真的看到 2 个候选
    after = _grabbable(lib)["S01E09"]
    assert before.summary != after.summary                        # 「1 个候选」→「2 个候选」
    assert history.fingerprint(before) == history.fingerprint(after)


def test_no_mikan_page_is_reported_per_season(lib):
    """找不到番组页（season 级，没有 path）：两季各一条、指纹各不相同。"""
    lib.tmdb.enabled = True
    sh = lib.show(SHOW)
    sh.season(1).local(f"{SHOW} S01E01.mkv")
    sh.season(2).local(f"{SHOW} S02E01.mkv")
    sh.season(2).local(f"{SHOW} S02E02.mkv")
    # 两季缺的集不同：摘要一样时 `Finding.key()` 按 (番, 摘要) 去重会只剩一条（这条摘要里不写季号）
    sh.tmdb(1234, seasons={1: weekly(12, first_days_ago=60), 2: weekly(12, first_days_ago=58)})
    sh.sidecar(seasons={"1": {"have": [1]}, "2": {"have": [1, 2]}})
    lib.web.mikan_search(SHOW, [])

    fs = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if "番组页" in f.summary]

    assert sorted(f.subject for f in fs) == ["S01", "S02"]
    assert len({history.fingerprint(f) for f in fs}) == 2


def _incomplete(lib) -> dict[str, object]:
    return {f.subject: f for f in lib.diagnose(detectors=[IncompleteSeasonDetector])}


def test_incomplete_seasons_have_distinct_fingerprints_that_survive_the_count(lib):
    lib.tmdb.enabled = True
    sh = lib.show(SHOW)
    s1, s2 = sh.season(1), sh.season(2)
    for n in range(1, 6):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    for n in range(1, 3):
        s2.local(f"{SHOW} S02E{n:02d}.mkv")
    sh.tmdb(1234, seasons={1: weekly(12, first_days_ago=60), 2: weekly(12, first_days_ago=40)})
    a = _incomplete(lib)
    assert set(a) == {"S01", "S02"}
    assert history.target_of(a["S01"]) == f"{SHOW}#S01"
    assert history.fingerprint(a["S01"]) != history.fingerprint(a["S02"])

    s1.local(f"{SHOW} S01E06.mkv")                               # 「缺 4 集」→「缺 3 集」
    b = _incomplete(lib)
    assert a["S01"].summary != b["S01"].summary
    assert history.fingerprint(a["S01"]) == history.fingerprint(b["S01"])


def test_phantom_only_slot_is_identified_by_the_slot(lib):
    """两个种子都说下完了、盘上都没有：path 是桶里第一个（哪个排第一取决于种子列表的顺序）。"""
    s1 = lib.show(SHOW).season(1)
    s1.single("[A] Yani Neko - 08 [1080p].mkv", size=GB, on_disk=False)
    s1.single("[B] Yani Neko - 08 [1080p].mkv", size=GB + 1, on_disk=False)

    [f] = [f for f in lib.diagnose(detectors=[DuplicateEpisodeDetector]) if f.kind == "phantom_only"]

    assert history.target_of(f) == f"{SHOW}#S01E08"


def test_pending_ownership_slot_is_identified_by_the_slot(lib):
    s1 = lib.show(SHOW).season(1)
    s1.single("[A] Yani Neko - 08 [1080p].mkv", size=GB, category="Bangumi")
    s1.single("[B] Yani Neko - 08 [1080p].mkv", size=GB + 1)

    [f] = [f for f in lib.diagnose(detectors=[DuplicateEpisodeDetector])
           if f.kind == "pending_ownership"]

    assert history.target_of(f) == f"{SHOW}#S01E08"


def test_each_empty_category_has_its_own_fingerprint(lib):
    """空分类没有番、没有路径：没有 subject，每个空分类共用一个指纹（一个 ack 静音全部）。"""
    s1 = lib.show(SHOW).season(1)
    s1.single("[A] Yani Neko - 07 [1080p].mkv", category="Bangumi")
    s1.single("[A] Yani Neko - 08 [1080p].mkv", category="旧分类")

    fs = [f for f in lib.diagnose(detectors=[CategoryConsolidationDetector])
          if f.kind == "empty_category"]

    assert sorted(history.target_of(f) for f in fs) == ["#分类:Bangumi", "#分类:旧分类"]
    assert len({history.fingerprint(f) for f in fs}) == 2
