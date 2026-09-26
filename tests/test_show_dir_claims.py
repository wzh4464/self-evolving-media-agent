"""目录改名（`rename_show_dir`）与它的回退，目的地先问占用。

以前正向只看 `new.exists()`、回退只看 `back.exists()`。目的地在盘上还不存在，不等于
没人占：AutoBangumi 按新标题建的订阅、或手动加的种子，save_path 可能已经指到那个目录下
（0%、甚至还没拿到元数据），或者一个 save_path 在媒体根、根文件夹恰好叫这个名字的
Original 布局种子。setLocation 把我们的种子搬进去，两边的文件就混在一个目录里，
同名的集位互相争（critic N6 在目录一级的形态）。
"""
from __future__ import annotations

import httpx
import pytest

from media_agent.kernel import Action, Finding


def _dir_rename(lib, old: str, new: str) -> Finding:
    p = lib.path(old)
    return Finding(rule="title-drift", kind="title_drift", severity="important",
                   summary=f"目录名 `{old}` 与 TMDB 官方标题 `{new}` 不一致", show=old,
                   path=str(p), action=Action(op="rename_show_dir",
                                              args={"path": str(p), "new_name": new}))


def _moves(lib) -> list[tuple]:
    return [c for c in lib.qbit.calls if c[0] == "set_location"]


# ------------------------------------------------------------------ 正向
def test_rename_into_a_dir_a_torrent_already_points_at_is_refused(lib):
    """新目录盘上还不存在，但一个 0% 的种子的 save_path 已经在它下面。"""
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    stranger = lib.show("别处").season(1).single("新名 S01E02.mkv", size=2000, progress=0.0)
    lib.qbit.raw(stranger.hash)["save_path"] = str(lib.path("新名/Season 1"))
    assert not lib.path("新名").exists()
    before = lib.snapshot()

    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])

    [skip] = rep.skipped
    assert "别的种子" in skip["reason"] and stranger.hash[:8] in skip["reason"]
    assert _moves(lib) == [] and lib.snapshot() == before


def test_rename_into_a_dir_a_root_level_torrent_fills_is_refused(lib):
    """save_path 在媒体根、Original 布局的根文件夹就叫新名：条目落在新目录下。"""
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    root = lib.show("_").folder("")
    stranger = root.torrent({"Season 1/新名 S01E03.mkv": 3000}, name="新名",
                            layout="original", progress=0.0)
    lib.qbit.raw(stranger.hash)["save_path"] = str(lib.media_root)

    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])

    [skip] = rep.skipped
    assert stranger.hash[:8] in skip["reason"]
    assert _moves(lib) == []


def test_rename_onto_a_case_only_different_existing_dir_is_refused(lib, fs):
    """大小写不敏感的卷上 `GNOSIA` 就是 `Gnosia`：按"已存在，需人工合并"处理。"""
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    lib.show("Gnosia").season(1).single("Gnosia S01E01.mkv", size=5000)

    rep = lib.apply([_dir_rename(lib, "旧名", "GNOSIA")])

    [skip] = rep.skipped
    assert "已存在" in skip["reason"]
    assert _moves(lib) == []


@pytest.mark.allow("failed_record", match="占用")
def test_rename_is_refused_when_occupancy_cannot_be_read(lib):
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    lib.qbit.fail("torrents", exc=httpx.ReadTimeout("timed out (injected)"))
    before = lib.snapshot()

    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])

    [rec] = rep.failed
    assert "未做任何改动" in rec["error"]
    assert lib.snapshot() == before


def test_sanity_rename_with_only_its_own_torrents_still_works(lib):
    show = lib.show("旧名")
    t = show.season(1).single("旧名 S01E01.mkv", size=1000)

    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])

    [rec] = rep.applied
    assert rec["torrents_moved"] == 1
    assert lib.qbit.torrent(t.hash)["save_path"] == str(lib.path("新名/Season 1"))


# ------------------------------------------------------------------ 回退
def test_rollback_into_an_old_dir_a_torrent_now_points_at_is_refused(lib):
    """改名之后，有个种子（按旧标题的订阅）把 save_path 指回了旧目录——它还没落盘。"""
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])
    [rec] = rep.applied
    assert not lib.path("旧名").exists()
    stranger = lib.show("别处").season(1).single("旧名 S01E02.mkv", size=2000, progress=0.0)
    lib.qbit.raw(stranger.hash)["save_path"] = str(lib.path("旧名/Season 1"))
    before = lib.snapshot()

    res = lib.rollback(rec["run_id"])

    assert res["reverted"] == 0 and res["skipped"] == 1 and res["failed"] == 0
    assert stranger.hash[:8] in res["skipped_detail"][0]["skip_reason"]
    assert lib.snapshot() == before


# ------------------------------------------------------------------ 残留搬运认得出"只差大小写"
def _case_insensitive(tmp_path) -> bool:
    probe = tmp_path / "case-probe"
    probe.write_text("x", encoding="utf-8")
    return (tmp_path / "CASE-PROBE").exists()


def test_rollback_leftover_merge_recognizes_a_claimed_file_whose_case_differs(lib, tmp_path):
    """回退的残留搬运（`_merge_tree`）绕开"此刻有种子声明"的文件。以前逐字比较路径：
    盘上的名字与 qBittorrent 的条目只差大小写时（APFS 上就是同一个文件），认不出它归种子，
    在 qBit 的异步搬运完成之前就用文件系统把它搬走了（AGENTS.md 第 3 条）。"""
    if not _case_insensitive(tmp_path):
        pytest.skip("只在大小写不敏感的卷上有意义（生产与 macOS 开发机）")
    show = lib.show("旧名")
    t = show.season(1).single("旧名 S01E01.mkv", size=1000)
    ident = lib.ident(t.path)
    rep = lib.apply([_dir_rename(lib, "旧名", "新名")])
    [rec] = rep.applied
    on_disk = lib.path("新名/Season 1/旧名 S01E01.mkv")
    renamed = on_disk.with_name("旧名 s01e01.MKV")
    on_disk.rename(renamed)                          # 盘上只改了大小写（种子条目没变）
    lib.qbit.async_moves = True

    res = lib.rollback(rec["run_id"])

    assert res["reverted"] == 1
    assert renamed.exists() and lib.ident(renamed) == ident   # 还在原地，等 qBit 自己搬
    assert not lib.path("旧名/Season 1/旧名 s01e01.MKV").exists()
