"""订阅模式（`AB_MODE=subscription`）下 `Bangumi` 分类不再归 AutoBangumi（`abmode` 模块文档）。

`full` 模式下判重对 `Bangumi` 分类让位：AB 的改名线程每 60 秒扫这个分类里下完的种子，文件此刻叫什么只是"AB 认为的"
（2026-08-31：AB 把 `3rd Season - 08` 改成 `S01E08`，判重把 2016 年真正的第 8 集清进了隔离区）。所以
- 集位里有 `Bangumi` 的候选、又没有封存：整个集位这一轮不做取舍（`pending_ownership`）；
- 封存了的集位里，`Bangumi` 的输家要发布名独立认出这个集位才清（`_release_agrees` 的 holdback）。

订阅模式下 AB 的改名线程停了：`Bangumi` 里的种子只会是订阅那一刻 AB 补的集，名字就是发布名、没人再改。让位的理由没了，
留着它只会让新订阅补来的重复一直等到分类交接之后（6 小时一轮的 `run` 里多一次迭代）。`full` 模式照旧——那是回退的路。
"""
from __future__ import annotations

from harness import video

from media_agent.plugins.builtin import DuplicateEpisodeDetector

SHOW = "尼古喵喵"
LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _ab_duplicate(lib):
    """已交接的旧版本（ABEMA 生肉）占着集位名；AB 订阅那一刻又补了带双字幕的 LoliHouse，还在 `Bangumi` 分类下。"""
    s1 = lib.show(SHOW).season(1)
    raw = s1.single(f"{SHOW} S01E08.mkv", size=745_065_995, name=ABEMA_08, probe=video("h264"))
    sub = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS, category="Bangumi")
    return raw, sub


def test_full_mode_still_defers_the_slot_to_ab(lib):
    _ab_duplicate(lib)
    fs = lib.diagnose(detectors=[DuplicateEpisodeDetector])
    assert [f.kind for f in fs] == ["pending_ownership"]


def test_subscription_mode_dedupes_a_bangumi_candidate_right_away(lib):
    lib.configure(ab_mode="subscription")
    raw, sub = _ab_duplicate(lib)

    fs = lib.diagnose(detectors=[DuplicateEpisodeDetector])

    assert "pending_ownership" not in [f.kind for f in fs]
    [t] = [f for f in fs if f.action and f.action.op == "trash"]
    assert t.torrent_hash == raw.hash and t.action.args["keep_hash"] == sub.hash


def test_subscription_mode_does_not_hold_back_a_bangumi_loser_in_a_sealed_slot(lib):
    """封存的集位（本项目抓的、复核通过）里另有一份 `Bangumi` 的，发布名认不出这一集（合集）：`full` 下留着等交接，
    订阅模式下没有谁会再改它的名字，照常当输家。"""
    s1 = lib.show(SHOW).season(1)
    s1.single(f"{SHOW} S01E08.mkv", size=593_601_176, name=LOLI_08, probe=TWO_SUBS, tags="ma:S01E08")
    s1.single("[Raws] Yani Neko - 08 [1080p].mkv", size=700_000_000, name="[Raws] Yani Neko [01-12][1080p]",
              probe=video("h264"), category="Bangumi")

    full = lib.diagnose(detectors=[DuplicateEpisodeDetector])
    assert "pending_ownership" in [f.kind for f in full]

    lib.configure(ab_mode="subscription")
    fs = lib.diagnose(detectors=[DuplicateEpisodeDetector])
    assert "pending_ownership" not in [f.kind for f in fs]
    assert [f.action.op for f in fs if f.action] == ["trash"]
