"""订阅模式（`AB_MODE=subscription`）下本项目不再替 AutoBangumi 做事（`abmode`）：

- **抓取不打 `ab:` 标签**：`full` 下抓来的集在 AB 会算出同一个集号时带 `ab:<订阅 id>`，好让 AB 认领；订阅模式下 AB 不改名，
  标签只会让"AB 在订阅之外加了种子"的核对（`abmode.activity`）把本项目抓的认成 AB 的。
"""
from __future__ import annotations

from harness import MikanItem, weekly

TITLE = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _grab_scene(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show("尼古喵喵")
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", bangumi_id=32, seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title=TITLE, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=["尼古喵喵"])
    return item


def test_full_mode_grab_still_tags_for_ab(lib):
    item = _grab_scene(lib)
    lib.cycle()
    assert {t.strip() for t in lib.qbit.torrent(item.infohash)["tags"].split(",")} == {"ma:S01E09", "ab:32"}


def test_subscription_mode_grab_does_not_tag_for_ab(lib):
    lib.configure(ab_mode="subscription")
    item = _grab_scene(lib)
    lib.cycle()
    assert lib.qbit.torrent(item.infohash)["tags"] == "ma:S01E09"
