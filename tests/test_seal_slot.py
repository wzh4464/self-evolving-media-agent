"""回归测试：media-agent 自己挑中并复核通过的那一份，判重不得再换掉。

2026-09-11 的真实误删——抓取器按 preferences 为《尼古喵喵》S01E10 选了
一个合并发布（TV 版 + 邪龙解放版装在同一个种子里，`无修` 命中 +100），
判重随后按画质/体积规则删掉了其中的邪龙解放版。审计里留下：

    保留: 【7月】尼古喵喵 10【TV版】.mp4
    清理: 【7月】尼古喵喵 10【邪龙解放版】.mp4

（更正：2026-09-18 一度以为 E01–E10 因此全成了 TV 版。按 infohash 核对，
库里那十集就是 LoliHouse 的邪竜解放版——它的种子内部名不标版本，
`[LoliHouse] Yani Neko - 08 [...]`，凭名字认不出来。）

根因不是排序写错了：`_rank_for_keep` 比的是画质、字幕轨、体积，
而「无删减」「特定字幕组」这类择源诉求它根本表达不了。让画质规则去
覆盖择源的结论，等于择源白做。所以有 `ma:` 钉子且复核通过的那一份
直接封存集位，不参与排名。

跑法：uv run pytest tests/test_seal_slot.py

第 10、11 组依赖仓库里的 `.agents/preferences.json`（`邪龙解放版` 只写在 JSON
里，`DEFAULT` 没有）——生产跑的就是这份文件，所以回归测试有意钉住它。
"""
import pytest

from harness import video
from media_agent import preferences
from media_agent.kernel import Show
from media_agent.plugins.builtin import (_prefer_score, _release_agrees,
                                        meets_requirements)

XIE = ("[LoliHouse] 尼古喵喵 (邪竜解放版) / ヤニねこ / Yani Neko / Chainsmoker Cat"
       " - 10 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
TV = "[LoliHouse] Yani Neko - 10 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
LOLI_ASS = ("[LoliHouse] Super no Ura de Yani Suu Futari - 09 "
            "[WebRip 1080p HEVC-10bit AAC ASSx2]")


@pytest.fixture
def show(tmp_path):
    # 目录不存在 → sidecar 读成默认值（没有 season_offsets）
    return Show(dir_name="尼古喵喵", dir_path=tmp_path / "尼古喵喵")


@pytest.fixture
def unprobed(make_file):
    """路径不存在的文件：probe 探不到 → 只凭发布名（旧脚本里的 /tmp/does-not-exist）。"""
    def _mk(filename, torrent_name="", **kw):
        return make_file(filename, torrent_name, exists=False, **kw)
    return _mk


# ---- 1–4：复核（meets_requirements）不是橡皮图章，也不能误杀 ----
def test_xie_release_passes_review(unprobed):
    """1) 邪竜解放版过硬门槛（名字里有「简繁内封」）。"""
    ok, why = meets_requirements(unprobed("尼古喵喵 S01E10.mkv", XIE))
    assert ok, why


def test_abema_raw_without_chinese_fails_review(unprobed):
    """2) 没有任何中文字幕证据的片源必须被挡下——2026-09-05 那个 711MB
    零字幕轨的 ABEMA 转载版。"""
    abema = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV) [0B743B5B]"
    ok, why = meets_requirements(unprobed("尼古喵喵 S01E08.mkv", abema))
    assert not ok, why


def test_lolihouse_assx2_name_alone_fails_when_unprobed(unprobed):
    """3) LoliHouse 的 `ASSx2` / `SRTx2` 命名不含任何硬门槛关键词，「简繁内封字幕」
    只在 Mikan 站点标题里。**只看名字会把整个组判死**——探不到时这一半确实会发生。"""
    ok, why = meets_requirements(unprobed("x.mkv", LOLI_ASS))
    assert not ok, why


def test_lolihouse_assx2_passes_when_file_has_chinese_tracks(make_file):
    """3 的另一半：文件里实实在在躺着两条中文字幕轨，就必须通过。
    先看文件、再看名字——2026-09-18 初版反过来写，36 个钉子里误判了 12 个。"""
    f = make_file("x.mkv", LOLI_ASS, probe=video("hevc", subs=["chi 简体中文", "chi 繁體中文"]))
    ok, why = meets_requirements(f)
    assert ok, why
    assert "内封字幕轨" in why


def test_hardsub_counts_as_acceptable(unprobed):
    """4) 内嵌硬字幕探不到轨道，不能因此判不合格。"""
    ok, why = meets_requirements(unprobed(
        "x.mkv", "[樱桃花字幕组]尼古喵喵 Yanineko - 10 [1080p][简日内嵌]"))
    assert ok, why


# ---- 5–9：封存快车道前，发布名必须独立确认集位（_release_agrees）----
def test_release_name_confirms_slot(unprobed, show):
    """5) TV 版的发布名认得出第 10 集 → 可以当轮清理。"""
    assert _release_agrees(unprobed("尼古喵喵 S01E10.mkv", TV), show, 1, 10) is True


def test_ab_misrenamed_candidate_is_not_confirmed(unprobed, show):
    """6) 关键守卫：AB 把 `3rd Season - 08` 改成了 `S01E08`，文件名骗人，
    发布名不骗人——2026-08-31 的 1.31GB 原片就是这么没的。"""
    lying = unprobed("Re：从零开始的异世界生活 S01E08.mkv",
                     "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 "
                     "[WebRip 1080p HEVC-10bit AAC][简繁内封]",
                     category="Bangumi")
    assert _release_agrees(lying, show, 1, 8) is False


def test_the_season_offset_is_applied_not_just_looked_up(unprobed, tmp_path):
    """6 的生产形态（2026-09-26 审查）：Re:Zero 的 sidecar 带着 `season_offsets {"3": 50}`。
    以前"声明的季有偏移"就放行、从不换算——`3rd Season - 08` 被当成 S01E08 的确认，而按
    `_resolve` 的算法它是 S01E58：封存快车道会把真正的第 58 集当第 8 集的输家清掉。"""
    from media_agent import sidecar as sc_mod

    d = tmp_path / "Re：从零开始的异世界生活"
    d.mkdir()
    sc_mod.save(d, sc_mod.Sidecar(season_offsets={"3": 50}))
    show = Show(dir_name=d.name, dir_path=d)
    lying = unprobed("Re：从零开始的异世界生活 S01E08.mkv",
                     "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p]",
                     category="Bangumi")

    assert _release_agrees(lying, show, 1, 8) is False
    assert _release_agrees(lying, show, 1, 58) is True


def test_episode_mismatch_is_not_confirmed(unprobed, show):
    """7) 集号对不上的也拒绝。"""
    assert _release_agrees(unprobed("x.mkv", TV), show, 1, 9) is False


def test_batch_release_is_not_confirmed(unprobed, show):
    """8) 合集种子的发布名解析不出单集集号 → 拒绝确认，不许当轮删。"""
    batch = unprobed("x.mkv", "[LoliHouse] Yani Neko [01-12][WebRip 1080p HEVC-10bit AAC]")
    assert _release_agrees(batch, show, 1, 10) is False


def test_local_file_without_release_name_is_not_confirmed(unprobed, show):
    """9) 没有发布名（纯本地文件）→ 拒绝确认。"""
    f = unprobed("尼古喵喵 S01E10.mkv")
    f.torrent_name = ""
    assert _release_agrees(f, show, 1, 10) is False


# ---- 10：合并发布里两个文件共用 torrent_name，必须靠文件名分高下 ----
BUNDLE = "[TV版&无修版] 尼古喵喵 - EP11 [简／繁] (1080p H.264 AAC SRTx2)"


@pytest.fixture
def bundle_pair(unprobed):
    tv = unprobed("【7月】尼古喵喵 11【TV版】.mp4", BUNDLE, size=738434422,
                  tags="ab:32, ma:S01E11")
    xie = unprobed("【7月】尼古喵喵 11【邪龙解放版】.mp4", BUNDLE, size=738563085,
                   tags="ab:32, ma:S01E11")
    return tv, xie


def test_bundle_prefers_uncut_by_filename(bundle_pair):
    """10) 否则并列、随机留下 TV 版——2026-09-18 EP11 实测就是这么选错的。"""
    tv, xie = bundle_pair
    assert _prefer_score(xie) > _prefer_score(tv), \
        "邪龙版 %s  vs  TV版 %s" % (_prefer_score(xie)[:2], _prefer_score(tv)[:2])


def test_bundle_members_share_review_verdict(bundle_pair):
    """两者复核结论一致（硬门槛看的是种子标题）。"""
    tv, xie = bundle_pair
    assert meets_requirements(tv)[0] == meets_requirements(xie)[0] is True


def test_bundle_sizes_are_indistinguishable(bundle_pair):
    """体积几乎一样（738.4MB vs 738.6MB），靠体积分不出来——这正是必须用偏好分的原因。"""
    tv, xie = bundle_pair
    assert abs(tv.size - xie.size) / tv.size < 0.001


# ---- 11：按番指定版本（「只保留邪竜解放版」），只在抓取选源时把关 ----
@pytest.fixture
def xie_only():
    return preferences.with_requirement(
        preferences.load_rules(), ["邪竜解放版", "邪龙解放版"], name="只保留邪竜解放版")


LOLI_XIE_12 = {"title": "[LoliHouse] 尼古喵喵 (邪竜解放版) / ヤニねこ / Yani Neko / "
                        "Chainsmoker Cat - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"}
NEST_12 = {"title": "[NEST] 尼古喵喵 / ヤニねこ / Chainsmoker Cat - 12 "
                    "[NF WEB-DL 1080p AVC AAC][简繁日内封]"}
BUNDLE_12 = {"title": "[TV版&无修版] 尼古喵喵 - EP12 [简／繁] (1080p H.264 AAC SRTx2)"}
SFB_12 = {"title": "[SFBgm] 尼古喵喵 / Yani Neko - 12 [WebRip 1080p AVC AAC CHT MP4]"}


def test_nf_cut_passes_global_but_not_per_show_gate(xie_only):
    """NF 删减版分数不低，但过不了按番门槛。"""
    assert not preferences.evaluate(NEST_12["title"], xie_only).acceptable
    assert preferences.evaluate(NEST_12["title"]).acceptable


def test_bundle_with_tv_cut_is_rejected_by_per_show_gate(xie_only):
    """合并发布也不收（里面带着 TV 版）。"""
    assert not preferences.evaluate(BUNDLE_12["title"], xie_only).acceptable


def test_only_lolihouse_xie_is_picked(xie_only):
    """四选一只会选 LoliHouse 邪竜解放版。"""
    best, _ = preferences.pick_best([NEST_12, SFB_12, BUNDLE_12, LOLI_XIE_12], xie_only)
    assert best is LOLI_XIE_12


def test_nothing_is_picked_before_xie_is_out(xie_only):
    """邪竜版还没出时宁可不抓。"""
    best, _ = preferences.pick_best([NEST_12, SFB_12, BUNDLE_12], xie_only)
    assert best is None


def test_shows_without_gate_are_unaffected():
    """没设按番门槛的番不受影响。"""
    rules = preferences.with_requirement(preferences.load_rules(), [])
    assert rules is not None
    assert preferences.pick_best([NEST_12], rules)[0] is NEST_12
