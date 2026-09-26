"""失联种子的重新关联（`relink_torrent`）不能关联到别的活种子的文件上（critic N6）。

`StaleTorrentPathDetector` 按"字节数唯一"在剧目录（目录没了就全库）里给失联种子找
文件，从不看那个文件是不是已经归另一个活种子。`relink_torrent` 随后 setLocation +
renameFile + recheck：两个种子从此声明同一个文件；大小相同而内容不同时，recheck
把分片判成缺失，qBittorrent 会把它们重新下载**覆盖到另一个种子的文件上**。

逆操作同理：把种子映射回原来（失联时）的路径，那个路径此后若被别的种子声明、
或盘上有了别的文件，回退就造出同样的双重声明。
"""
from __future__ import annotations

import httpx
import pytest

from media_agent.plugins.builtin import StaleTorrentPathDetector

SIZE = 734_003_200
OLD = "GNOSIA - S01E08 [WebRip 1080p HEVC-10bit AAC].mkv"
SLOT = "古诺希亚 S01E08.mkv"


def _stale(s1, **kw):
    """INC-04 的现场：种子说已下完，它记着的文件名在盘上已经没有了。"""
    return s1.single(OLD, size=SIZE, on_disk=False, **kw)


def _relink_calls(lib) -> list[tuple]:
    return [c for c in lib.qbit.calls
            if c[0] in ("set_location", "rename_file", "recheck")]


# ------------------------------------------------------------------ 正向
def test_sanity_relink_onto_an_unclaimed_file_still_works(lib):
    s1 = lib.show("古诺希亚").season(1)
    t = _stale(s1)
    f = s1.local(SLOT, size=SIZE)

    c = lib.cycle(detectors=[StaleTorrentPathDetector])

    [rec] = c.applied("relink_torrent")
    assert rec["relinked"] == 1
    assert lib.qbit.file_names(t.hash) == [SLOT]
    assert lib.qbit.torrent(t.hash)["progress"] == 1          # recheck 通过
    assert f.exists()


def test_relink_onto_a_file_another_torrent_owns_is_refused(lib):
    """N6 原样：同样大小的文件正归另一个活种子 B。"""
    s1 = lib.show("古诺希亚").season(1)
    t = _stale(s1)
    b = s1.single(SLOT, size=SIZE, name="[B] Gnosia - 08 [1080p].mkv")
    before = lib.snapshot()

    c = lib.cycle(detectors=[StaleTorrentPathDetector])

    [skip] = c.skipped("relink_torrent")
    assert "另一个" in skip["reason"] and b.hash[:8] in skip["reason"]
    assert [x["hash"] for x in skip["claims"][0]["claimants"]] == [b.hash]
    assert _relink_calls(lib) == []
    assert lib.snapshot() == before
    assert lib.qbit.file_names(t.hash) == [OLD]


def test_relink_into_another_dir_onto_a_claimed_file_is_refused(lib):
    """目录被整个挪走的情形：新 save_path 下那个文件归别的种子，setLocation 都不能发。"""
    sh = lib.show("古诺希亚")
    t = _stale(sh.season(1))
    b = sh.season(2).single(SLOT, size=SIZE, name="[B] Gnosia - 08 [1080p].mkv")
    before = lib.snapshot()

    c = lib.cycle(detectors=[StaleTorrentPathDetector])

    [skip] = c.skipped("relink_torrent")
    assert skip["args"]["new_save_path"] == str(sh.path / "Season 2")   # 确实是换目录的那种
    assert b.hash[:8] in skip["reason"]
    assert _relink_calls(lib) == [] and lib.snapshot() == before
    assert lib.qbit.torrent(t.hash)["save_path"] == str(sh.path / "Season 1")


@pytest.mark.allow("failed_record", match="占用")
def test_relink_is_refused_when_occupancy_cannot_be_read(lib):
    s1 = lib.show("古诺希亚").season(1)
    _stale(s1)
    s1.local(SLOT, size=SIZE)
    neighbour = s1.single("古诺希亚 S01E07.mkv", size=SIZE + 1)
    findings = lib.diagnose(detectors=[StaleTorrentPathDetector])
    lib.qbit.fail("files", hash=neighbour.hash, times=None,
                  exc=httpx.ReadTimeout("timed out (injected)"))

    rep = lib.apply(findings)

    [rec] = rep.failed
    assert "未做任何改动" in rec["error"] and neighbour.hash[:8] in rec["error"]
    assert _relink_calls(lib) == []


# ------------------------------------------------------------------ 逆操作
def test_sanity_relink_rolls_back(lib):
    s1 = lib.show("古诺希亚").season(1)
    t = _stale(s1)
    s1.local(SLOT, size=SIZE)
    c = lib.cycle(detectors=[StaleTorrentPathDetector])

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert lib.qbit.file_names(t.hash) == [OLD]


def test_relink_undo_refuses_to_map_back_onto_a_path_someone_now_claims(lib):
    """回退要把种子映射回原来的名字，而那个名字此后被一个新种子（0%）占了。"""
    s1 = lib.show("古诺希亚").season(1)
    t = _stale(s1)
    s1.local(SLOT, size=SIZE)
    c = lib.cycle(detectors=[StaleTorrentPathDetector])
    newcomer = s1.torrent({OLD: SIZE}, name="[G] Gnosia - 08 (re-grab)", layout="single",
                          progress=0.0)
    before = lib.snapshot()

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1 and res["failed"] == 0
    [d] = res["skipped_detail"]
    assert newcomer.hash[:8] in d["skip_reason"]
    assert lib.snapshot() == before
    assert lib.qbit.file_names(t.hash) == [SLOT]
