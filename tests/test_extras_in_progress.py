"""还在下载的特典（NCOP / NCED / PV …）要立刻设为不下载，而不是等它下完再隔离。

ExtrasDetector 不排除没下完的条目：scan 的来源 1 按 `torrents/files` 给出干净的目标名
（盘上是 `.!qB` 或者什么都没有），检测器照样对它提 `trash{file_only}`。main 上
`_op_trash` 先把条目优先级设成 0，qBittorrent 就不再下它。第 2 条修复（隔离前先核对
文件存在）之后，这一步在"路径不存在 → 跳过"里被一起跳过了：特典照下不误——占带宽、
占一个 94% 满的 APFS 容器的空间（critic N8）——下完之后下一轮才进隔离区；停滞的合集
则每一轮都写一条理由是「种子声明了但盘上没有，或诊断后被挪走」的误导性跳过记录。
单文件的 PV / CM 种子同理：main 上立刻摘掉（生产 20260830T132317 那一条）。
"""
from __future__ import annotations

from media_agent.plugins.builtin import ExtrasDetector

GB = 600_000_000


def _pack(lib, **kw):
    s1 = lib.show("银八").season(1)
    return s1, s1.torrent({"银八 S01E01.mkv": GB, "NCOP1.mkv": 90_000_000},
                          name="[G] Gintama BD", layout="original",
                          state="downloading", **kw)


def _priorities(lib, t):
    return [(f["name"], f["priority"]) for f in lib.qbit.raw(t.hash)["_files"]]


def test_extra_inside_a_downloading_pack_is_set_to_skip_at_once(lib):
    s1, t = _pack(lib, progress=0.5)
    ep = s1.path / "[G] Gintama BD" / "银八 S01E01.mkv.!qB"
    ident = lib.ident(ep)

    c = lib.cycle(detectors=[ExtrasDetector])

    [rec] = c.applied("trash")
    assert rec["priority_zeroed"] and rec["undo"]["op"] == "restore_file_priority"
    assert "trash_path" not in rec["undo"]
    assert _priorities(lib, t) == [("[G] Gintama BD/银八 S01E01.mkv", 1),
                                   ("[G] Gintama BD/NCOP1.mkv", 0)]
    assert lib.qbit.has(t.hash)
    assert lib.ident(ep) == ident                          # 正片的半成品一个字节没动
    assert not c.skipped("trash")


def test_nothing_on_disk_yet_is_handled_the_same_way(lib):
    _, t = _pack(lib, progress=0.0)

    c = lib.cycle(detectors=[ExtrasDetector])

    assert c.applied("trash") and not c.skipped("trash")
    assert _priorities(lib, t)[1] == ("[G] Gintama BD/NCOP1.mkv", 0)


def test_stalled_pack_does_not_repeat_a_skip_every_round(lib):
    _pack(lib, progress=0.5)

    rounds = lib.converge(detectors=[ExtrasDetector])

    assert not any(r.skipped("trash") for r in rounds)
    assert not any(r.failed() for r in rounds)


def test_single_file_extra_torrent_in_progress_is_dropped(lib):
    s1 = lib.show("银八").season(1)
    t = s1.single("[G] Gintama PV.mkv", size=90_000_000, progress=0.3)

    c = lib.cycle(detectors=[ExtrasDetector])

    [rec] = c.applied("trash")
    assert rec["undo"]["op"] == "readd_torrent" and rec["files_untouched"]
    assert not lib.qbit.has(t.hash)


def test_priority_zero_rolls_back(lib):
    _, t = _pack(lib, progress=0.5)
    c = lib.cycle(detectors=[ExtrasDetector])

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert [p for _, p in _priorities(lib, t)] == [1, 1]


def test_completed_extra_is_still_moved_to_trash(lib):
    """下完的特典照旧：设为不下载 + 移入隔离区（行为不变）。"""
    s1 = lib.show("银八").season(1)
    t = s1.torrent({"银八 S01E01.mkv": GB, "NCOP1.mkv": 90_000_000},
                   name="[G] Gintama BD", layout="original")

    c = lib.cycle(detectors=[ExtrasDetector])

    [rec] = c.applied("trash")
    assert rec["undo"]["op"] == "restore_from_trash"
    assert _priorities(lib, t)[1][1] == 0 and len(lib.trash_files()) == 1
