"""同一批次里，前面的动作已经处置掉的东西，后面的动作要认得出来。

testinfra B2 / LAT-05：诊断一次性全量产出，执行按动作顺序排：trash（op 5）在
rename（op 6）之前。判重把发布名输家整种子作废后，unrenamed-file 仍会对这个
输家提"改成规范名"——轮到它时种子已经删了，`files()` 返回 404，审计记成
`failed`。生产上 5 次（20260909T024309、20260911T091146 ×2、20260916T102034、
20260918T145907），而且喂进 `find_failure_patterns`，被当成"规则本身有问题"。

同一形态的另一半：输家没有种子时，trash 已经把它搬走，rename 先查"目标名被占"
（赢家刚改好名），报一条误导的「集位被占」（20260924T173911、20260924T234117）。
"""
from __future__ import annotations

import pytest

from harness import video
from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import UnrenamedDetector

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def test_rename_of_a_torrent_deleted_earlier_in_the_batch_is_skipped(lib):
    """B2 原样：两个发布名版本，输家整种子作废，随后它的改名不能记 failed。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single(ABEMA_08, size=745_065_995, probe=video("h264"))
    winner = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)

    c = lib.cycle()

    [trash] = c.applied("trash")
    assert trash["args"]["torrent_hash"] == loser.hash
    assert not c.failed()
    [skip] = [r for r in c.skipped("rename") if r["args"]["torrent_hash"] == loser.hash]
    assert "本批次" in skip["reason"]
    [ren] = c.applied("rename")
    assert ren["args"]["torrent_hash"] == winner.hash
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176}


def test_local_loser_trashed_earlier_is_not_reported_as_slot_occupied(lib):
    """纯本地的输家被搬走后，它的改名不能报「集位被占」——那会把人引到判重上去查。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("[ASub] Yani Neko - 08 [1080p].mkv", size=593_601_176, probe=TWO_SUBS)
    loser = s1.local("[ZRaw] Yani Neko - 08 [1080p].mkv", size=700_000_000,
                     probe=video("h264"))

    c = lib.cycle()

    assert [r["args"]["path"] for r in c.applied("trash")] == [str(loser)]
    [skip] = [r for r in c.skipped("rename") if r["args"]["path"] == str(loser)]
    assert "集位被占" not in skip["reason"] and "本批次" in skip["reason"]


def _trash(path, h, **kw):
    return Finding(rule="t", kind="duplicate", severity="important", summary=str(path),
                   show="尼古喵喵", path=str(path), torrent_hash=h,
                   action=Action(op="trash", args={"path": str(path), "torrent_hash": h, **kw}))


def test_second_trash_on_a_torrent_already_removed_this_batch(lib):
    """合集里只剩一集还要下载（NCOP 早先已设为不下载、文件还留在盘上）：那一集判重输了，
    整种子作废（只剩它一个要下的文件，删除关口的 I3 不降级）；同批又要把 NCOP 移进隔离区——
    种子已经没了，不能再去问它的文件列表（404 → failed），文件照常进隔离区。

    （以前的现场是"合集里一集判重输了就整种子作废"——那正是 I3 堵上的洞，见
    `tests/test_deletion_gate.py`；要下载的文件多于一个时不再整种子作废。）"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"a/E05.mkv": 600_000_000, "b/NCOP.mkv": 90_000_000},
                      name="[G] Yani Neko 01-12", layout="nosub",
                      priorities={"b/NCOP.mkv": 0})
    e05, ncop = pack.paths
    s1.local("尼古喵喵 S01E05.mkv")                       # 这一集的保留方

    rep = lib.apply([_trash(e05, pack.hash), _trash(ncop, pack.hash, file_only=True)])

    assert not rep.failed and len(rep.applied) == 2
    assert not lib.qbit.has(pack.hash)
    assert not e05.exists() and not ncop.exists()
    assert rep.applied[0]["undo"]["torrent_record_lost"] is True
    assert rep.applied[1]["undo"]["torrent_record_lost"] is False   # 丢记录只算一次
    after = lib.qbit.calls[lib.qbit.calls.index(("delete", (pack.hash,), False)):]
    assert ("files", pack.hash) not in after                       # 没去问已删种子


def test_complete_torrent_file_missing_on_disk_is_not_renamed(lib):
    """种子说已下完、盘上却没有：改名只会把一个幻影映射到集位名上。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI_08, size=593_601_176, on_disk=False)

    c = lib.cycle()

    [skip] = c.skipped("rename")
    assert "不在" in skip["reason"]
    assert lib.qbit.file_names(t.hash) == [LOLI_08]


def test_downloading_file_not_on_disk_yet_is_still_renamed(lib):
    """下载中（0%，盘上还没有）的改名是支持的功能，不能被上一条误伤。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI_08, size=593_601_176, progress=0.0)

    c = lib.cycle()

    assert c.applied("rename")
    assert lib.qbit.file_names(t.hash) == ["尼古喵喵 S01E08.mkv"]


# ------------------------------------------------------------------ 各道闸单独的现场
# 上面 B2 原样那条，输家的路径同时也在 `_trashed_paths` 里：删掉 `_removed_torrents`
# 那道检查，"文件本批次已移入隔离区"照样满足 `"本批次" in reason`，测试仍绿
# （审查变异：h5_rename_removed_off / h5_drop_add_removed_off 都存活）。
# 下面每条只让一道闸有机会拦住它。

def test_rename_of_a_torrent_dropped_as_dead_earlier_in_the_batch_is_skipped(lib):
    """drop_torrent（op 1）摘掉死种、没有搬任何文件；它的改名（op 6）只能靠
    `_removed_torrents` 认出来，否则去问已删种子的 `files()`——404。"""
    s1 = lib.show("尼古喵喵").season(1)
    dead = s1.torrent({"[A] Yani Neko - 11 [1080p].mkv": 600_000_000},
                      name="[A] Yani Neko - 11 [1080p].mkv", layout="single",
                      progress=0.4, state="stalledDL", added_hours_ago=24 * 30,
                      num_complete=0, availability=0)

    c = lib.cycle()

    [drop] = c.applied("drop_torrent")
    assert drop["args"]["torrent_hash"] == dead.hash
    [skip] = [r for r in c.skipped("rename") if r["args"]["torrent_hash"] == dead.hash]
    assert "本批次" in skip["reason"] and "移除" in skip["reason"]
    assert not c.failed()


@pytest.mark.allow("qbit_error", match="404")
def test_rename_of_a_torrent_deleted_outside_the_batch_is_skipped_not_failed(lib):
    """诊断之后、执行之前有人手动删了种子：`files()` 404 是状态变了，不是规则错了。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI_08, size=593_601_176)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    lib.qbit.delete([t.hash], delete_files=False)

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert "已不在 qBittorrent" in skip["reason"]
    assert not rep.failed and (s1.path / LOLI_08).exists()


def test_rename_of_a_local_file_that_vanished_after_diagnose_is_skipped(lib):
    lib.configure(qbit_allow_empty=True)             # 纯本地文件的库
    s1 = lib.show("尼古喵喵").season(1)
    loose = s1.local("[ZRaw] Yani Neko - 08 [1080p].mkv", size=700_000_000)
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    loose.unlink()

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert "不在原位" in skip["reason"]
    assert not rep.failed
