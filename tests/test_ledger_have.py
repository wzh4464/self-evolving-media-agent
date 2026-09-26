"""已有的集与在下的集，集号口径统一（critic N14）：钉子与出处账本认得出的文件，还没改名也算"已有"。

以前 `have_episodes` / `episode_of_file` 只看名字，`_inflight` 看钉子；钉着 `ma:S01E08`、还叫着发布名 `- 03`
的已下完文件（发布方的编号与库内不同）在 `have` 里算成第 3 集，第 8 集于是"缺"、被再抓一遍——409、再改一次
显示名、再改一次文件名。生产上 28 个 infohash 被抓了不止一次（42 次多余的抓取，入间 S4E20 一集 7 次），
这是来源之一。`_inflight` 反过来：已经改成规范名的在下文件按名字认、不看钉子。

现在两边同一个口径：钉子 > 账本（抓取器定的集位；声明了季号、按此刻换算得出的）> 名字。账本认不出的照旧按名字。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent import ledger
from media_agent.kernel import have_episodes
from media_agent.plugins.grab import EpisodeAvailableDetector, _inflight

SHOW = "尼古喵喵"
H8 = "9" * 40


def _grabs(findings) -> dict[str, object]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def _library(lib, *, eight_tags="ma:S01E08", eight_name="[Fyy Raws] Yani Neko 2nd Cour - 03 [1080p].mkv"):
    """E1–E7 已改名；第 8 集已下完、钉着 ma:S01E08，还叫着发布名（发布方按分段编号写成 `- 03`）。
    番组页上第 8 集有一个可抓的发布。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 8):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    schedule = weekly(8, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, mikan_id="3500", seasons={"1": {"have": list(range(1, 8))}})
    eight = s1.single(eight_name, hash=H8, tags=eight_tags)
    item = MikanItem(title="[LoliHouse] 尼古喵喵 / Yani Neko - 08 [WebRip 1080p][简繁内封字幕]",
                     pub=dict(schedule)[8])
    lib.mikan("3500", [item], search=[SHOW])
    return sh, eight


def _scan_show(lib):
    state = lib.scan()
    [show] = [s for s in state.shows if s.dir_name == SHOW]
    return state, show


def test_a_pinned_unrenamed_completed_file_counts_as_have(lib):
    _library(lib)

    state, show = _scan_show(lib)

    assert 8 in have_episodes(show)[1]
    assert 8 in have_episodes(show, allow_release_names=False)[1]     # sidecar-sync 的口径也认钉子
    assert 3 in have_episodes(show)[1]                                 # 第 3 集本来就在（已改名的那份）
    assert not _grabs(lib.diagnose(state, detectors=[EpisodeAvailableDetector]))


def test_a_ledger_identified_file_counts_as_have_without_a_pin(lib):
    """钉子没了（人改过标签）：账本里抓取器定的集位照样认。"""
    _library(lib, eight_tags="")
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H8, mikan_title="[Fyy Raws] 尼古喵喵 / Yani Neko 2nd Cour - 03 [1080p]",
                        season=1, episode=8, run_id="g0")

    state, show = _scan_show(lib)

    assert 8 in have_episodes(show, allow_release_names=False)[1]
    assert not _grabs(lib.diagnose(state, detectors=[EpisodeAvailableDetector]))


def test_without_pin_or_ledger_the_name_decides(lib):
    """对照：钉子与账本都没有——按名字它就是第 3 集，第 8 集照抓（与以前一样）。"""
    _library(lib, eight_tags="")

    state, show = _scan_show(lib)

    assert 8 not in have_episodes(show)[1]
    assert set(_grabs(lib.diagnose(state, detectors=[EpisodeAvailableDetector]))) == {"S01E08"}


def test_inflight_prefers_the_pin_over_a_normalized_name(lib):
    """在下的文件已经被改成 `S01E03`（别人按错口径改的），钉子说它是第 8 集：在下的是第 8 集。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    s1.single(f"{SHOW} S01E03.mkv", hash=H8, tags="ma:S01E08", progress=0.4, active_hours_ago=1)

    state, show = _scan_show(lib)

    busy = _inflight(lib.context(), show, {t["hash"]: t for t in state.torrents})
    assert busy == {1: {8}}


def test_a_grab_row_on_a_two_video_torrent_does_not_name_both_files(lib):
    """账本只替单视频的种子说话——`recorded_slot` 与 `ledger_view` 同一道闸：合集 / 合并发布的一行说不了单个文件。
    没有这道闸，抓取行的 (1, 8) 会把同一个种子里的第 9 集也算成第 8 集：`have` 少一集、`_inflight` 也少一集
    （2026-09-27 审查：去掉 `recorded_slot` 里这一条的变异全套存活，`ledger_view` 的那一条有测试）。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    done = s1.torrent({"[G] Yani Neko - 08 [1080p].mkv": 600_000_000, "[G] Yani Neko - 09 [1080p].mkv": 600_000_000},
                      name="[G] Yani Neko 08-09", layout="nosub", hash=H8)
    busy = s1.torrent({"[G] Yani Neko - 10 [1080p].mkv": 600_000_000, "[G] Yani Neko - 11 [1080p].mkv": 600_000_000},
                      name="[G] Yani Neko 10-11", layout="nosub", hash="8" * 40, progress=0.4,
                      active_hours_ago=1)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=done.hash, mikan_title="[G] 尼古喵喵 / Yani Neko 08-09", season=1, episode=8)
        led.record_grab(infohash=busy.hash, mikan_title="[G] 尼古喵喵 / Yani Neko 10-11", season=1, episode=10)

    state, show = _scan_show(lib)

    assert {8, 9} <= have_episodes(show)[1]
    # 在下的合集：名字（`10-11`）也说不了单个文件是哪一集——不能因为抓取行写着 (1, 10) 就把两个文件都算成第 10 集
    assert _inflight(lib.context(), show, {t["hash"]: t for t in state.torrents}) == {}
