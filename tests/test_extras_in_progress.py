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


KUSURIYA = "Kusuriya no Hitorigoto"


def _bd_pack(lib, progress, extra_files=None):
    """药屋的 BD 合集：正片 `[01]`–`[03]`，特典带集号（生产上真有：`[menu][S01E03]`、
    `[PV][S01E01]`；吊带袜 `[menu][01]` / `[PV][05]`；K-ON Moozzi2 `Menu - 01..09`）。"""
    s1 = lib.show("药屋少女的呢喃").season(1)
    files = {f"{KUSURIYA}[{n:02d}][1080P].mkv": GB for n in (1, 2, 3)}
    files.update(extra_files if extra_files is not None else {
        f"{KUSURIYA}[menu][S01E03][1080P].mkv": 90_000_000,
        f"{KUSURIYA}[PV][S01E01][1080P].mkv": 90_000_000})
    return s1, s1.torrent(files, name=f"[VCB-Studio] {KUSURIYA}", layout="original",
                          progress=progress, state="downloading" if progress < 1 else None)


def test_numbered_extras_inside_a_downloading_pack_are_set_to_skip_at_once(lib):
    """2026-09-26 审查（回归）：`ExtrasDetector._only_copy` 只数**下完了的**同集正片。合集还在下时，
    那一集的正片就是合集自己的成员、也没下完——带集号的特典于是被当成那一集唯一的文件，不提处置，
    照下不误（main 上立刻设为不下载）。生产隔离区里就有这些特典的 `.!qB`（orphanqb-20260830）。"""
    _, t = _bd_pack(lib, 0.5)

    c = lib.cycle(detectors=[ExtrasDetector])

    assert len(c.applied("trash")) == 2
    zeroed = sorted(n.rsplit("/", 1)[-1] for n, p in _priorities(lib, t) if p == 0)
    assert zeroed == [f"{KUSURIYA}[PV][S01E01][1080P].mkv",
                      f"{KUSURIYA}[menu][S01E03][1080P].mkv"]


def test_a_downloading_episode_with_a_marker_in_its_name_keeps_downloading(lib):
    """对照：发布名的标题里带记号的**正片**（罗马音认不出标题），同一集没有别的——哪怕没下完，
    也不能设为不下载。"""
    s1 = lib.show("拖车公园").season(1)
    t = s1.torrent({"[G] Trailer Park Boys - 05 [1080p].mkv": GB,
                    "[G] Trailer Park Boys - 06 [1080p].mkv": GB},
                   name="[G] Trailer Park Boys 05-06", layout="nosub", progress=0.5,
                   state="downloading")

    c = lib.cycle(detectors=[ExtrasDetector])

    assert not c.actions("trash")
    assert all(p == 1 for _, p in _priorities(lib, t))
