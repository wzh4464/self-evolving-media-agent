"""改名（`_op_rename`）与逆改名走共用的占用闸门，并按完整相对路径认种子条目。

第 1 阶段的 `_op_rename` 查两样：盘上 `target.exists()`，以及另一个种子的条目路径与
目标**逐字符相等**（`_claimants`）。漏掉的：

- 盘上只有别人的 `X.!qB`（种子已摘的孤儿半成品，或死种留下的）：下载中的文件改名是
  `A.!qB → X.!qB`，libtorrent 撞上已有的 `X.!qB`——覆盖或报 "File exists"。
- 只差大小写 / Unicode 规范化的声明：生产卷是大小写不敏感的 APFS，`s01e08.MKV` 就是
  `S01E08.mkv`，逐字符比较看不见。
- 反过来，自己的文件只改大小写：APFS 上 `target.exists()` 为真（就是它自己），
  被误报成「集位被占」，永远改不成。

`_torrent_rel_path` 以前按**文件名**找条目：合集里不同子目录下同名的文件
（`a/E05.mkv`、`b/E05.mkv`），会把另一个条目改掉。第 1 阶段只修了 file_only 隔离那一处。
"""
from __future__ import annotations

import httpx
import pytest

from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import UnrenamedDetector

GB = 600_000_000
SLOT = "尼古喵喵 S01E08.mkv"
RAW = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"


def _rename(path, new_name, h="") -> Finding:
    return Finding(rule="unrenamed-file", kind="unrenamed", severity="important",
                   summary=f"{path.name} → {new_name}", show="尼古喵喵", path=str(path),
                   torrent_hash=h,
                   action=Action(op="rename", args={"path": str(path), "new_name": new_name,
                                                    "torrent_hash": h}))


def _renames(lib) -> list[tuple]:
    return [c for c in lib.qbit.calls if c[0] == "rename_file"]


# ------------------------------------------------------------------ 盘上的半成品
def test_downloading_file_is_not_renamed_onto_an_orphan_partial(lib):
    """死种摘了记录、`X.!qB` 留在盘上；新种子还在下，改名会让两份半成品撞在一起。"""
    s1 = lib.show("尼古喵喵").season(1)
    dead = s1.single(SLOT, size=GB, progress=0.4, name="[Old] Yani Neko - 08.mkv")
    lib.qbit.delete([dead.hash], delete_files=False)
    me = s1.single(RAW, size=GB, progress=0.5)
    before = lib.snapshot()

    c = lib.cycle(detectors=[UnrenamedDetector])

    [skip] = c.skipped("rename")
    assert "集位被占" in skip["reason"] and "半成品" in skip["reason"]
    assert skip["claimants"][0]["partial"] is True
    assert lib.snapshot() == before and _renames(lib) == []
    assert lib.qbit.file_names(me.hash) == [RAW]


# ------------------------------------------------------------------ 大小写 / 规范化
def test_rename_refuses_a_case_only_collision_with_another_torrent(lib):
    """另一个 0% 的种子已映射到 `s01e08.MKV`——在 APFS 上就是目标本身。"""
    s1 = lib.show("尼古喵喵").season(1)
    other = s1.single("尼古喵喵 s01e08.MKV", size=GB, progress=0.0)
    me = s1.single(RAW, size=GB)

    rep = lib.apply([_rename(me.path, SLOT, me.hash)])

    [skip] = rep.skipped
    assert "声明" in skip["reason"] and other.hash[:8] in skip["reason"]
    assert _renames(lib) == [] and lib.qbit.file_names(me.hash) == [RAW]


def test_case_only_rename_of_own_torrent_file_is_applied(lib):
    """自己的文件只改大小写：盘上"已存在"的正是它自己，不是集位被占。"""
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single("尼古喵喵 s01e08.mkv", size=GB)
    ident = lib.ident(me.path)

    rep = lib.apply([_rename(me.path, SLOT, me.hash)])

    [rec] = rep.applied
    assert rec["via"] == "qbittorrent"
    assert lib.qbit.file_names(me.hash) == [SLOT]
    assert lib.ident(s1.path / SLOT) == ident
    assert [p.name for p in s1.path.iterdir()] == [SLOT]


def test_case_only_rename_of_own_local_file_is_applied(lib):
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    loose = s1.local("尼古喵喵 s01e08.mkv", size=GB)

    rep = lib.apply([_rename(loose, SLOT)])

    [rec] = rep.applied
    assert rec["via"] == "filesystem"
    assert [p.name for p in s1.path.iterdir()] == [SLOT]


# ------------------------------------------------------------------ fail closed
@pytest.mark.allow("failed_record", match="占用")
def test_rename_is_refused_when_the_torrent_list_cannot_be_read(lib):
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single(RAW, size=GB)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    lib.qbit.fail("torrents", exc=httpx.ReadTimeout("timed out (injected)"))

    rep = lib.apply(findings)

    [rec] = rep.failed
    assert "无法确认" in rec["error"] and "未做任何改动" in rec["error"]
    assert _renames(lib) == [] and lib.qbit.file_names(me.hash) == [RAW]


@pytest.mark.allow("failed_record", match="占用")
def test_rename_is_refused_when_a_neighbours_file_list_cannot_be_read(lib):
    """同目录的另一个种子读不到文件列表：它可能正声明着目标名。"""
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single(RAW, size=GB)
    neighbour = s1.single("[X] Yani Neko - 09.mkv", size=GB)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    lib.qbit.fail("files", hash=neighbour.hash, times=None)

    rep = lib.apply([f for f in findings if f.torrent_hash == me.hash])

    [rec] = rep.failed
    assert neighbour.hash[:8] in rec["error"]
    assert _renames(lib) == []


# ------------------------------------------------------------------ 按完整相对路径认条目
def _twin_pack(s1, first="a/E05.mkv", second="b/E05.mkv"):
    return s1.torrent({first: GB, second: GB + 1}, name="[G] Yani Neko 05 twins",
                      layout="nosub")


def test_rename_picks_the_entry_by_full_relative_path_not_by_file_name(lib):
    s1 = lib.show("尼古喵喵").season(1)
    pack = _twin_pack(s1)
    a, b = pack.paths
    ident_b = lib.ident(b)

    rep = lib.apply([_rename(b, "尼古喵喵 S01E05.mkv", pack.hash)])

    [rec] = rep.applied
    assert lib.qbit.file_names(pack.hash) == ["a/E05.mkv", "b/尼古喵喵 S01E05.mkv"]
    assert lib.ident(s1.path / "b" / "尼古喵喵 S01E05.mkv") == ident_b
    assert a.exists()


def test_undo_rename_picks_the_entry_by_full_relative_path(lib):
    """回退时按文件名找，会把另一个子目录里同名的条目改回去。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = _twin_pack(s1, first="a/尼古喵喵 S01E05.mkv")
    _, b = pack.paths
    lib.apply([_rename(b, "尼古喵喵 S01E05.mkv", pack.hash)], run_id="fw")
    assert lib.qbit.file_names(pack.hash) == ["a/尼古喵喵 S01E05.mkv", "b/尼古喵喵 S01E05.mkv"]

    res = lib.rollback("fw")

    assert res["reverted"] == 1
    assert lib.qbit.file_names(pack.hash) == ["a/尼古喵喵 S01E05.mkv", "b/E05.mkv"]


def test_rename_of_a_path_the_torrent_does_not_list_is_still_refused(lib):
    """按完整路径找不到就报失败——绝不退化成文件系统改名（AGENTS.md 第 3 条）。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = _twin_pack(s1)
    stray = s1.local("c/E05.mkv", size=GB)

    rep = lib.apply([_rename(stray, "尼古喵喵 S01E05.mkv", pack.hash)])

    [rec] = rep.failed
    assert "找不到该文件" in rec["error"]
    assert stray.exists() and _renames(lib) == []


# 上一条会写一条 failed 审计；它就是要验证的行为
test_rename_of_a_path_the_torrent_does_not_list_is_still_refused = pytest.mark.allow(
    "failed_record", match="找不到该文件")(
    test_rename_of_a_path_the_torrent_does_not_list_is_still_refused)


# ------------------------------------------------------------------ 执行器的占用索引：一批次一份、改完就作废
# 2026-09-26 审查：唯一的相关测试（test_path_claims 的 stale index）直接测 ClaimIndex，执行器怎么接的
# 没人测——`_audit` 不再作废索引（A31）、索引拷一份而不是按引用拿本批次摘掉的种子（A34），全套照样全绿。
def test_two_renames_onto_one_name_in_one_batch_the_second_is_refused(lib):
    """两个都是 0% 的种子（盘上什么都没有），同一批里都要改到同一个集位名：第一个改完，只有
    qBittorrent 知道这个名字已经有主——第二个要看到它，就得用一份新的索引（以前缓存的文件列表
    里还是旧名字：2026-08-29 / 2026-09-06 两个种子争一个路径的形态）。"""
    s1 = lib.show("尼古喵喵").season(1)
    t1 = s1.single("[A] Yani Neko - 05.mkv", size=GB, progress=0.0, state="downloading",
                   on_disk=False)
    t2 = s1.single("[B] Yani Neko - 05.mkv", size=GB, progress=0.0, state="downloading",
                   on_disk=False)
    x = "尼古喵喵 S01E05.mkv"

    rep = lib.apply([_rename(t1.path, x, t1.hash), _rename(t2.path, x, t2.hash)])

    assert len(rep.applied) == 1 and len(rep.skipped) == 1, (rep.applied, rep.skipped)
    assert "集位被占" in rep.skipped[0]["reason"]
    names = [n for h in (t1.hash, t2.hash) for n in lib.qbit.file_names(h)]
    assert names.count(x) == 1, names


def test_a_torrent_dropped_earlier_in_the_batch_does_not_claim_while_qbit_still_lists_it(lib):
    """qBittorrent 的删除是异步的：死种摘掉之后，它还会在列表里出现一会儿、仍"声明"着集位名。
    占用索引按引用拿执行器的 `_removed_torrents`，同一批后面的动作（隔离它的半成品、把新文件
    改到这个名字上）不能被一个已经摘掉的种子挡住。"""
    from media_agent.plugins.builtin import DeadTorrentDetector

    lib.qbit.async_deletes = True
    s1 = lib.show("尼古喵喵").season(1)
    dead = s1.torrent({SLOT: GB}, name="[Old] Yani Neko - 08.mkv", layout="single",
                      progress=0.4, state="stalledDL", added_hours_ago=24 * 30,
                      availability=0, num_complete=0)
    me = s1.local(RAW, size=GB)
    findings = lib.diagnose(detectors=[DeadTorrentDetector]) + [_rename(me, SLOT)]

    rep = lib.apply(findings)

    assert lib.qbit.has(dead.hash)                         # 还在列表里（删除没落地）
    assert sorted(r["op"] for r in rep.applied) == ["drop_torrent", "rename", "trash"], \
        (rep.applied, rep.skipped)
    assert (s1.path / SLOT).exists()
