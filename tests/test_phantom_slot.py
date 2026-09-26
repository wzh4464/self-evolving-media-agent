"""幻影（种子说已下完、盘上却没有）不能占着集位，也不能赢下判重。

LAT-01 的余波：生产 2026-09-19 那轮 qBit 登录超时时，朱音落语 S01E12 的文件被当成
本地文件移进了隔离区，它的种子 d08f05a7 却还在——此后这个种子就是一个幻影：
`torrents/files` 报着 `朱音落语 S01E12.mp4`、进度 100%，盘上什么都没有。用户经
Jellyfin 删文件、手工挪文件，同样会造出幻影。

第 2 条修复（`_op_trash` 先核对文件再碰种子）之后，判重对幻影输家的隔离变成
"跳过、种子与文件都不动"。于是同一批里：输家 P 的种子还在，`_op_rename` 只看磁盘上
`target.exists()`，把赢家 K 经 renameFile 改到 P 仍在声明的那个名字上——
**两个种子宣称同一个路径**。此后每轮都无声无息：scan 每个路径只出一条 MediaFile，
colliding-torrent 又不管两个都 100% 的种子。main 上 P 的记录会在同一轮被摘掉
（只是写下了一条 `trash_path: ""` 的坏逆操作）。

复现时还发现另一半更糟：幻影的声明大小更大、发布名更好时，它在判重里**赢**，
真文件 K 作为输家被移进隔离区，这一集从库里消失，幻影永远留着。钉了 `ma:` 的幻影
同理会被封存（探测不到文件时 `meets_requirements` 只看发布名）。
"""
from __future__ import annotations

from media_agent.plugins.builtin import UnrenamedDetector

GB = 500_000_000
K_RAW = "[ANi] Akane-banashi - 12 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
SLOT = "朱音落语 S01E12.mp4"


def _akane(lib):
    s1 = lib.show("朱音落语").season(1)
    for e in range(1, 12):
        s1.local(f"朱音落语 S01E{e:02d}.mp4", size=GB)
    return s1


def _claims(lib, *handles) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in handles:
        if lib.qbit.has(t.hash):
            for p in t.current_paths():
                out.setdefault(str(p), []).append(t.hash)
    return out


def test_phantom_loser_record_is_dropped_and_keeper_takes_the_slot(lib):
    """审查报告的原样现场：P 是幻影输家，K 是发布名的真文件。"""
    s1 = _akane(lib)
    p = s1.single(SLOT, size=508_000_000, on_disk=False)
    k = s1.single(K_RAW, size=508_100_000)
    ident = lib.ident(k.path)

    rounds = lib.converge()

    first = rounds[0]
    [drop] = first.applied("trash")
    assert drop["args"]["torrent_hash"] == p.hash and drop["args"].get("phantom")
    assert drop["undo"]["op"] == "readd_torrent"          # 不是 restore_from_trash
    assert "trash_path" not in drop["undo"]
    assert not first.failed()
    assert not lib.qbit.has(p.hash)
    assert _claims(lib, p, k) == {str(s1.path / SLOT): [k.hash]}
    assert lib.ident(s1.path / SLOT) == ident
    assert lib.trash_files() == []                         # 什么文件都没进隔离区


def test_phantom_never_wins_the_duplicate_even_with_a_better_name(lib):
    """幻影声明得更大、发布名更好：以前它赢，唯一的真文件被移进隔离区。"""
    s1 = _akane(lib)
    p = s1.single(SLOT, name="[Better] Akane-banashi - 12 [1080p].mp4", size=900_000_000,
                  on_disk=False)
    k = s1.single(K_RAW, size=508_100_000)
    ident = lib.ident(k.path)

    lib.converge()

    assert lib.trash_files() == []
    assert not lib.qbit.has(p.hash)
    assert lib.ident(s1.path / SLOT) == ident
    assert _claims(lib, p, k) == {str(s1.path / SLOT): [k.hash]}


def test_pinned_phantom_is_never_sealed_over_a_real_file(lib):
    """钉了 `ma:S01E12` 的幻影：探测不到文件时复核只看发布名，以前会被封存成赢家。"""
    s1 = _akane(lib)
    p = s1.single(SLOT, name="[LoliHouse] Akane-banashi - 12 [WebRip 1080p HEVC-10bit AAC 简繁内封].mp4",
                  size=508_000_000, on_disk=False, tags="ma:S01E12")
    k = s1.single(K_RAW, size=508_100_000)
    ident = lib.ident(k.path)

    lib.converge()

    assert lib.trash_files() == []
    assert lib.ident(s1.path / SLOT) == ident
    assert not lib.qbit.has(p.hash)


def test_rename_refuses_a_target_another_torrent_still_claims(lib):
    """单看改名规则：目标名虽然不在盘上，但另一个活种子声明着它——不能改过去。"""
    s1 = _akane(lib)
    p = s1.single(SLOT, size=508_000_000, on_disk=False)
    k = s1.single(K_RAW, size=508_100_000)
    before = lib.snapshot()

    c = lib.cycle(detectors=[UnrenamedDetector])

    [skip] = c.skipped("rename")
    assert "声明" in skip["reason"] and p.hash[:8] in skip["reason"]
    assert lib.snapshot() == before
    assert lib.qbit.file_names(k.hash) == [K_RAW]


def test_local_file_is_not_renamed_onto_a_phantom_claim_either(lib):
    s1 = _akane(lib)
    s1.single(SLOT, size=508_000_000, on_disk=False)
    loose = s1.local("[Group] Akane-banashi - 12 [1080p].mp4", size=508_100_000)

    c = lib.cycle(detectors=[UnrenamedDetector])

    [skip] = c.skipped("rename")
    assert "声明" in skip["reason"]
    assert loose.exists() and not (s1.path / SLOT).exists()


def test_phantom_drop_rolls_back_to_the_same_claim(lib):
    s1 = _akane(lib)
    p = s1.single(SLOT, size=508_000_000, on_disk=False)
    s1.single(K_RAW, size=508_100_000)
    c = lib.cycle(select=lambda f: f.action and f.action.op == "trash")
    assert not lib.qbit.has(p.hash)

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert lib.qbit.has(p.hash)
    assert lib.qbit.torrent(p.hash)["save_path"] == str(s1.path)


# ------------------------------------------------------------------ 执行器单测
def _phantom_trash(path, h, **kw):
    from media_agent.kernel import Action, Finding
    args = {"path": str(path), "torrent_hash": h, "phantom": True, **kw}
    return Finding(rule="duplicate-episode", kind="duplicate", severity="important",
                   summary="x", show="朱音落语", path=str(path), torrent_hash=h,
                   action=Action(op="trash", args=args))


def test_phantom_flag_is_rechecked_downloading_again_is_left_alone(lib):
    """诊断时是幻影，执行前它又开始下载（`.!qB` 出现了）：不摘。"""
    s1 = lib.show("朱音落语").season(1)
    p = s1.single(SLOT, size=508_000_000, on_disk=False)
    (s1.path / (SLOT + ".!qB")).write_bytes(b"partial")
    before = lib.qbit.snapshot()

    rep = lib.apply([_phantom_trash(p.path, p.hash)])

    [s] = rep.skipped
    assert "不是幻影" in s["reason"]
    assert lib.qbit.snapshot() == before


def test_phantom_flag_is_rechecked_renamed_away_is_left_alone(lib):
    s1 = lib.show("朱音落语").season(1)
    p = s1.single(SLOT, size=508_000_000, on_disk=False)
    f = _phantom_trash(p.path, p.hash)
    lib.qbit.rename_file(p.hash, SLOT, "别的名字.mp4")      # 诊断之后改过名
    before = lib.qbit.snapshot()

    rep = lib.apply([f])

    assert [r["op"] for r in rep.skipped] == ["trash"]
    assert lib.qbit.snapshot() == before


def test_phantom_member_of_a_pack_only_gets_priority_zero_and_rolls_back(lib):
    """合集里的一个条目是幻影（file_only）：只设为不下载，其余照常做种；回退恢复优先级。"""
    s1 = lib.show("朱音落语").season(1)
    pack = s1.torrent({"朱音落语 S01E11.mp4": GB, "朱音落语 S01E12.mp4": GB},
                      name="[G] Akane-banashi 11-12", layout="nosub")
    e11, e12 = pack.paths
    e12.unlink()                                          # 用户删了一集
    before = lib.disk()

    rep = lib.apply([_phantom_trash(e12, pack.hash, file_only=True)], run_id="pz")

    [rec] = rep.applied
    assert rec["priority_zeroed"] and rec["undo"]["op"] == "restore_file_priority"
    assert lib.qbit.has(pack.hash) and lib.disk() == before
    assert [f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]] == [1, 0]

    res = lib.rollback("pz")

    assert res["reverted"] == 1
    assert [f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]] == [1, 1]
