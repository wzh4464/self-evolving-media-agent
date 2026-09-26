"""抓取的"换源放行"必须与死种判据同一口径：放行的那一轮，死种就要被摘掉。

`grab._inflight` 把"已经有种子在下"的集排除在抓取之外（两个种子抢同一个文件、谁也
校验不过，见 colliding-torrent），唯一的例外是**停滞太久**的——那种就放行、换个源抓。
main 上放行条件和 dead-torrent 的判死条件都是 `now - added_on > DEAD_TORRENT_HOURS`：
放行抓新源（op 0）和摘掉旧种子（op 1）发生在同一轮。

第 3 条修复把死种改成按"最后一次活着"（加入 / 最后收发 / 最后见到完整副本取最晚）计时，
`_inflight` 却还用 `added_on`。于是一个 72 小时前加入、10 小时前还收过数据的种子：
抓取放行、新种子被 `_rename_grabbed` 改到同一个集位名上，旧种子却不算死、不被摘——
两个种子抢同一个文件，colliding-torrent 只会报「谁也完不成」、不动手；旧种子只要还在
给别人上传分片，`last_activity` 就一直在刷新，永远等不到被摘。
"""
from __future__ import annotations

import time
from pathlib import Path

from harness import MikanItem, weekly

GB = 600_000_000
TITLE = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
RELEASE = TITLE.replace("/", "_") + ".mkv"          # libtorrent 规范化后的单文件名
SLOT = "尼古喵喵 S01E09.mkv"


def _scene(lib, **old_kw):
    sh = lib.show("尼古喵喵")
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title=TITLE, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=["尼古喵喵"])
    kw = dict(layout="single", progress=0.4, state="stalledDL", added_hours_ago=72,
              num_complete=0, availability=0)
    kw.update(old_kw)
    old = s1.torrent({"尼古喵喵 S01E09.mkv": GB}, name="[Old] Yani Neko - 09.mkv", **kw)
    return s1, old, item


def _claims(lib, s1) -> list[str]:
    """哪些种子此刻声明着 E09 的集位名。"""
    slot = s1.path / "尼古喵喵 S01E09.mkv"
    return sorted(h for h in lib.qbit.snapshot()
                  if slot in [Path(lib.qbit.raw(h)["save_path"]) / n
                              for n in lib.qbit.file_names(h)])


def test_old_but_recently_active_torrent_is_not_replaced(lib):
    """审查原样：加入 72h、10h 前还在收数据——它不是死种，就不该换源。"""
    s1, old, item = _scene(lib, active_hours_ago=10)

    c = lib.cycle()

    assert not c.actions("grab_episode")
    assert not lib.qbit.has(item.infohash)
    assert lib.qbit.has(old.hash)
    assert _claims(lib, s1) == [old.hash]


def test_truly_stalled_torrent_is_replaced_and_dropped_in_the_same_run(lib):
    s1, old, item = _scene(lib, active_hours_ago=72)

    c = lib.cycle()

    [grab] = c.applied("grab_episode")
    [drop] = c.applied("drop_torrent")
    assert drop["args"]["torrent_hash"] == old.hash
    assert not lib.qbit.has(old.hash)
    # 抓取（op 0）早于摘死种（op 1）：改名那一刻旧种子还声明着集位名、盘上还有它的
    # `X.!qB`。以前照改——两个种子争同一个文件，新种子接着往旧种子的半成品里写
    # （grab 调研 S1）。现在走占用闸门：留在发布名上，钉子留着。
    assert grab["rename"]["renamed"] is None
    assert old.hash in [c.get("hash") for c in grab["rename"]["claims"]["claimants"]]
    assert _claims(lib, s1) == []                         # 没有两个种子同时声明它
    assert lib.qbit.file_names(item.infohash) == [RELEASE]
    assert "ma:S01E09" in lib.qbit.torrent(item.infohash)["tags"]
    # 死种摘记录（op 1）之后，它自己的半成品经删除关口进了隔离区（op 5），不再占着集位名
    assert not (s1.path / (SLOT + ".!qB")).exists()
    [part] = c.applied("trash")
    assert part["args"]["path"] == str(s1.path / (SLOT + ".!qB"))
    assert part["deletion"]["disposition"] == "dead_partial"


def test_replacement_takes_the_slot_once_the_dead_partial_is_disposed_of(lib):
    """以前是 xfail：死种的 X.!qB 孤儿留在盘上占着集位名，新种子下完也改不过去（占用闸门按设计
    拦下）。删除关口把"种子已摘、没人认领的 .!qB"移进隔离区之后，新种子下完就拿到集位名。"""
    s1, old, item = _scene(lib, active_hours_ago=72)
    lib.cycle()
    lib.qbit.complete(item.infohash)

    lib.converge()

    assert lib.qbit.file_names(item.infohash) == [SLOT]
    assert not (s1.path / (SLOT + ".!qB")).exists()


def test_the_dead_partial_is_disposed_of_once_the_replacement_owns_the_name(lib):
    """故事的结尾（2026-09-26 审查，复现 7/7）：换源的新种子下完、改到集位名上，它以优先级 1 声明
    `X`、`X` 完整地在盘上。隔离区里那份 40% 的 `X.!qB` 属于另一个早已摘掉的死种——以前"原路径
    仍被种子声明"把它永远留着，每 6 小时在 run.log 里报一次"删了它那个种子就指着一个不存在的
    文件"（并不存在），94% 满的容器上这份空间永远腾不出来。"""
    from datetime import datetime, timedelta

    from media_agent import disposal

    s1, old, item = _scene(lib, active_hours_ago=72)
    lib.cycle()
    lib.qbit.complete(item.infohash)
    lib.converge()
    assert lib.qbit.file_names(item.infohash) == [SLOT]

    rep = disposal.dispose(lib.context(), mode="run", run_id="p900",
                           now=datetime.now() + timedelta(days=31))

    assert [c.trash_path.name for c in rep.deleted] == [SLOT + ".!qB"]
    assert (s1.path / SLOT).exists() and not rep.overdue


def test_stalled_but_seen_complete_recently_is_not_replaced(lib):
    s1, old, item = _scene(lib, active_hours_ago=72)
    lib.qbit.raw(old.hash)["seen_complete"] = int(time.time() - 3600)

    c = lib.cycle()

    assert not c.actions("grab_episode")
    assert lib.qbit.has(old.hash)


def test_dead_pack_with_completed_members_is_not_replaced(lib):
    """死种里有已下完的成员：dead-torrent 只报告、不摘——那抓取也不能放行，
    否则新旧两个种子同样会并存。"""
    sh = lib.show("尼古喵喵")
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title=TITLE, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=["尼古喵喵"])
    pack = s1.torrent({"尼古喵喵 S01E09.mkv": GB, "尼古喵喵 S01E10.mkv": GB},
                      name="[Old] Yani Neko 09-10", layout="nosub", progress=0.5,
                      state="stalledDL", added_hours_ago=72, active_hours_ago=72,
                      num_complete=0, availability=0)
    lib.qbit.raw(pack.hash)["_files"][1]["progress"] = 1

    c = lib.cycle()

    assert not [a for a in c.actions("grab_episode")
                if a.action.args.get("episode") == 9]
    assert lib.qbit.has(pack.hash)
