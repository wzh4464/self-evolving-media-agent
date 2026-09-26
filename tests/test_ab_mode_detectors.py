"""订阅模式（`AB_MODE=subscription`）下只为 AutoBangumi 存在的规则停下来（ab 调研 §2.1）；`full` 照旧——那是回退的路，
所以是按模式关，不是删。

| 规则 | 为什么只为 AB 存在 | 订阅模式下 |
|---|---|---|
| `orphan-torrent` / `missing-ab-tag` | 给种子补 `ab:<id>`，好让 AB 改名（它其实只按分类扫） | AB 不改名了：不报 |
| `title-match-broken`（→ `fix_title_aliases`） | AB 按子串匹配发布名，别名失效就一集不下 | AB 不拉 RSS 了：不报、不改 AB 库、不叫它刷新 |
| `source-abandoned`（→ `repoint_rss`） | AB 一条订阅锁一个字幕组、搜索式 RSS 会失效 | 同上 |
| `rename-collision` | "AB 每 60 秒重试同一个改名"的死循环（critical） | 仍报撞名（只有一份拿得到集位名），不再说死循环、降为 important |
"""
from __future__ import annotations

import pytest

from media_agent.plugins.builtin import OrphanTorrentDetector, RenameCollisionDetector
from media_agent.plugins.subscription import (MissingAbTagDetector, SourceAbandonedDetector,
                                              TitleMatchBrokenDetector)

AB_ONLY = [OrphanTorrentDetector, MissingAbTagDetector, TitleMatchBrokenDetector, SourceAbandonedDetector]


@pytest.mark.parametrize("cls", AB_ONLY, ids=lambda c: c.id)
def test_ab_only_rules_do_nothing_in_subscription_mode(lib, cls):
    """订阅模式下一进门就走：连扫描结果都不看（`state=None` 一碰就崩）——也就不拉 AB 的 RSS、不查番组页。"""
    lib.configure(ab_mode="subscription")
    lib.tmdb.enabled = True                    # source-abandoned 没有 TMDB 本来就走：让它非得靠模式的闸
    assert list(cls().detect(lib.context(), None)) == []
    lib.configure(ab_mode="full")
    with pytest.raises(AttributeError):        # 对照：full 下它真的会去看扫描结果
        list(cls().detect(lib.context(), None))


@pytest.mark.parametrize("cls", AB_ONLY, ids=lambda c: c.id)
def test_ab_only_rules_are_still_registered_for_the_revert_path(lib, cls):
    names = {getattr(d, "id", "") for d in lib.registry().detectors}
    assert cls.id in names


def _orphan_scene(lib):
    sh = lib.show("甲番")
    sh.bangumi(1, title_raw="Jia")
    s1 = sh.season(1)
    s1.single("[G] Jia - 01 [1080p].mkv", category="Bangumi")        # AB 订阅那一刻补的：没有标签
    s1.single("甲番 S01E02.mkv", name="[G] Jia - 02 [1080p].mkv")      # 人手加的：没有标签也没钉子


def test_the_tag_rules_fire_in_full_mode_and_not_in_subscription_mode(lib):
    _orphan_scene(lib)
    kinds = [f.kind for f in lib.diagnose(detectors=[OrphanTorrentDetector, MissingAbTagDetector])]
    assert "orphan_torrent" in kinds and "subscription_untagged" in kinds

    lib.configure(ab_mode="subscription")
    assert lib.diagnose(detectors=[OrphanTorrentDetector, MissingAbTagDetector]) == []


def test_title_match_broken_fires_in_full_mode_and_not_in_subscription_mode(lib):
    rss = "https://mikanani.me/RSS/Search?searchstr=Sake+o+Tsugu"
    lib.bangumi(id=7, official_title="上伊那牡丹", title_raw="Sake o Tsugu",
                save_path=str(lib.media_root / "上伊那牡丹" / "Season 1"), rss_link=rss, group_name="ANi")
    lib.show("上伊那牡丹").season(1).single("[ANi] Sake o Tsugu - 04.mp4", name="[ANi] Sake o Tsugu - 04")
    lib.web.rss(rss, [f"[ANi] 上伊那牡丹，醉身姿如百合 / Yoeru Sugata wa Yuri no Hana - {n:02d}"
                      f" [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4" for n in (5, 6, 7)])

    assert [f.action.op for f in lib.diagnose(detectors=[TitleMatchBrokenDetector]) if f.action] == \
           ["fix_title_aliases"]
    lib.configure(ab_mode="subscription")
    assert lib.diagnose(detectors=[TitleMatchBrokenDetector]) == []


def _collision(lib):
    s1 = lib.show("朱音落语").season(1)
    s1.single("[JPSC] Akane-banashi - 08 [1080p].mp4", size=600_000_001)
    s1.single("[JPTC] Akane-banashi - 08 [1080p].mp4", size=600_000_002)


def test_rename_collision_keeps_the_ab_loop_framing_in_full_mode(lib):
    _collision(lib)
    [f] = lib.diagnose(detectors=[RenameCollisionDetector])
    assert f.severity == "critical" and "死循环" in f.summary


def test_rename_collision_is_reported_generically_in_subscription_mode(lib):
    """AB 不再改名，谈不上"每 60 秒重试"：撞名照报（两份里只有一份拿得到集位名，另一份留在发布名上），不再是 critical；
    指纹不变（身份是集位，不含摘要），切模式不会让卡住检测断档。"""
    from media_agent import history

    _collision(lib)
    [full] = lib.diagnose(detectors=[RenameCollisionDetector])
    lib.configure(ab_mode="subscription")
    [f] = lib.diagnose(detectors=[RenameCollisionDetector])

    assert f.kind == "rename_collision" and f.severity == "important"
    assert "死循环" not in f.summary and "AutoBangumi" not in f.summary
    assert "只有一份" in f.summary
    assert history.fingerprint(f) == history.fingerprint(full)
