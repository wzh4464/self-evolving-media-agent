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

from harness import video
from media_agent.kernel import Action, Finding

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
    """合集里一集判重输了（整种子作废），同批又要只作废它的 NCOP：
    种子已经没了，不能再去问它的文件列表（404 → failed），文件照常进隔离区。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"a/E05.mkv": 600_000_000, "b/NCOP.mkv": 90_000_000},
                      name="[G] Yani Neko 01-12", layout="nosub")
    e05, ncop = pack.paths

    rep = lib.apply([_trash(e05, pack.hash), _trash(ncop, pack.hash, file_only=True)])

    assert not rep.failed and len(rep.applied) == 2
    assert not lib.qbit.has(pack.hash)
    assert not e05.exists() and not ncop.exists()
    assert rep.applied[1]["undo"]["torrent_record_lost"] is False   # 丢记录只算一次


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
