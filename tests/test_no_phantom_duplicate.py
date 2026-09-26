"""回归测试：同一磁盘路径不得产出重复删除动作。

2026-09-06 尼古喵喵 S01E08 因此丢过一次原件。两个种子宣称同一路径
（换版时旧种子被停用、文件移走，但 qBittorrent 里的记录还在，
新种子 renameFile 到同一集位文件名后两者重合），`scan` 为同一路径
发出两条 MediaFile，`duplicate-episode` 判定"这一集有 2 个文件"，
排序后清理"输的那个"——删掉的正是唯一的真文件。
审计里留下自相矛盾的一行：`保留 X，清理 X`。

原先第 1 项是对生产全库跑 `scan`（要登录真 qBittorrent，媒体卷不在时还会
空转通过）。现在拆成两半：
- 这里用 LibraryBuilder 把"两个种子宣称同一路径"的现场搭出来，离线验证；
- 全库不变量挪到 `tests/live/test_library_invariants.py`，默认不跑。

跑法：uv run pytest tests/test_no_phantom_duplicate.py
"""
from pathlib import Path

import pytest

from media_agent.kernel import LibraryState, MediaFile, Show
from media_agent.naming import looks_simplified, looks_traditional
from media_agent.plugins.builtin import DuplicateEpisodeDetector
from media_agent.probe import MediaInfo, size_for_compare

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
SLOT = "尼古喵喵 S01E08.mkv"


# ---- 1) scan：一个路径只产出一条 MediaFile，归磁盘大小对得上的那个种子 ----
@pytest.fixture(params=["stale_first", "new_first"])
def two_claims(request, lib):
    """换版现场：旧种子（ABEMA，已停用、文件已移走）与新种子（LoliHouse，
    renameFile 到集位名）的 `torrents/files` 报着同一个路径。两种登记顺序都要测——
    scan 按字典顺序遍历种子，归属不能取决于谁先被看到。"""
    s1 = lib.show("尼古喵喵").season(1)

    def stale():
        return s1.single(SLOT, size=745_065_995, name=ABEMA_08, state="stoppedUP",
                         on_disk=False)

    def new():
        return s1.single(SLOT, size=593_601_176, name=LOLI_08)

    if request.param == "stale_first":
        old, cur = stale(), new()
    else:
        cur, old = new(), stale()
    return s1, old, cur


def test_scan_emits_one_entry_per_path(lib, two_claims):
    s1, old, cur = two_claims
    state = lib.scan(resolve_tmdb=False)

    [show] = state.shows
    assert [str(f.path) for f in show.files] == [str(s1.path / SLOT)]
    assert show.files[0].torrent_hash == cur.hash          # 大小对得上的才是真主人
    assert show.files[0].size == 593_601_176


def test_full_cycle_never_trashes_the_only_real_file(lib, two_claims):
    s1, old, cur = two_claims
    before = lib.ident(s1.path / SLOT)

    c = lib.cycle()

    assert not [f for f in c.findings if f.kind == "duplicate"]
    assert not c.applied("trash")
    assert lib.ident(s1.path / SLOT) == before              # 唯一的真文件还在原位
    assert lib.trash_files() == []


# ---- 2) 规则护栏：即使上游真的发出了两条同路径条目，也不能产出删除动作 ----
def test_duplicate_detector_ignores_same_path_entries(lib):
    p = lib.media_root / "__phantom__" / "Season 1" / "__phantom__ S01E08.mkv"

    def mk(torrent_name: str, size: int, h: str) -> MediaFile:
        return MediaFile(
            path=p, size=size, show_dir="__phantom__", season_dir="Season 1",
            filename=p.name, torrent_hash=h, torrent_name=torrent_name,
            torrent_state="stalledUP", torrent_progress=1.0,
            torrent_tags="", torrent_category="__phantom__",
        )

    show = Show(dir_name="__phantom__", dir_path=p.parent.parent)
    show.files = [
        mk("[GroupA] Show - 08 (1920x1080 AVC AAC).mkv", 745065995, "a" * 40),
        mk("[GroupB] Show - 08 [1080p HEVC-10bit AAC SRTx2].mkv", 593601176, "b" * 40),
    ]
    st = LibraryState()
    st.shows = [show]

    found = list(DuplicateEpisodeDetector().detect(lib.context(), st))

    dels = [f for f in found if f.kind == "duplicate"]
    assert not dels, "共 %d 条 finding，删除类 %d 条" % (len(found), len(dels))


# ---- 3) 重复集取舍：字幕能力必须排在体积之前 ----
# 2026-09-05 尼古喵喵 S01E08：带简繁双字幕轨的 HEVC 566MB 输给了
# 零字幕轨的 AVC 710MB，因为排序键只看文件名、且裸比体积。
SUB_HEVC = MediaInfo(vcodec="hevc", height=1080, sub_count=2,
                     sub_marks=("chi 简体中文", "chi 繁體中文"))
RAW_AVC = MediaInfo(vcodec="h264", height=1080, sub_count=0, sub_marks=())


def test_probe_two_chinese_tracks_count_as_simplified():
    assert SUB_HEVC.has_simplified
    assert SUB_HEVC.subtitle_rank() == MediaInfo.SOFT_SIMPLIFIED, \
        "subtitle_rank=%d" % SUB_HEVC.subtitle_rank()


def test_probe_no_subtitle_tracks_rank_zero():
    assert RAW_AVC.subtitle_rank() == MediaInfo.NONE


def test_probe_jpsc_jptc_tracks_count_as_simplified():
    """2026-09-08 穹庐下的魔女 S01E11：字幕轨 title 写的是 JPSC/JPTC，
    `\\bsc\\b` 匹配不到（JP 和 SC 之间没有词边界），简繁双内封轨只被当成
    "普通中文"，与靠名字猜的内嵌同分，按体积输掉。"""
    jpsc = MediaInfo(vcodec="hevc", height=1080, sub_count=2,
                     sub_marks=("chi JPSC", "chi JPTC"))
    assert jpsc.has_simplified
    assert jpsc.subtitle_rank() == MediaInfo.SOFT_SIMPLIFIED, \
        "sub_marks=%s rank=%d" % (jpsc.sub_marks, jpsc.subtitle_rank())


def test_language_detection_accepts_english_full_words():
    """2026-09-15《抓娃娃》：Netflix/爱奇艺多语言片源的轨道 title 是英文全词
    `Simplified Chinese`，原先的正则只收中文字和 CHS/SC 缩写。
    文件名、发布标题、字幕轨 title 三种写法都要覆盖。"""
    assert looks_simplified("chi Simplified Chinese")
    assert looks_traditional("chi Traditional Chinese")
    assert not looks_simplified("eng English")


def test_multilingual_source_ranks_as_simplified():
    multi = MediaInfo(vcodec="h264", height=752, sub_count=13,
                      sub_marks=("chi Simplified Chinese", "chi Traditional Chinese",
                                 "eng English", "jpn Japanese"))
    assert multi.subtitle_rank() == MediaInfo.SOFT_SIMPLIFIED, "rank=%d" % multi.subtitle_rank()


def test_probed_soft_subs_outrank_name_guessed_hardsubs():
    """内封最好、内嵌也行——同分就把这个偏好抹平了。"""
    assert MediaInfo.SOFT_CHINESE > MediaInfo.HARD_SIMPLIFIED


def test_subtitle_capability_ranks_before_size():
    """带字幕的 HEVC 必须排在无字幕的 AVC 之前。"""
    assert (1080, SUB_HEVC.subtitle_rank()) > (1080, RAW_AVC.subtitle_rank())


def test_cross_codec_size_is_normalized():
    """跨编码体积折算：566MB HEVC 的等效画质应高于 710MB AVC。"""
    hevc_eq = size_for_compare(593601176, "hevc")
    avc_eq = size_for_compare(745065995, "h264")
    assert hevc_eq > avc_eq, "HEVC 566MB → %.0fMB 等效；AVC 710MB → %.0fMB 等效" % (
        hevc_eq / 2 ** 20, avc_eq / 2 ** 20)


def test_duplicate_resolution_uses_probed_facts(lib):
    """把上面的排序事实接回真实流程：两份真的同集文件，探测结果决定去留。"""
    from harness import video

    s1 = lib.show("尼古喵喵").season(1)
    raw = s1.single(ABEMA_08, size=745_065_995, probe=video("h264"))
    sub = s1.single(LOLI_08, size=593_601_176,
                    probe=video("hevc", subs=["chi 简体中文", "chi 繁體中文"]))

    [dup] = [f for f in lib.diagnose() if f.kind == "duplicate"]

    assert dup.path == str(raw.path)
    assert Path(dup.evidence["keep"]) == sub.path
