"""目录级搬运（`rename_show_dir` 的回退、`repair`）不能让任何活种子失联。

AGENTS.md 第 3 条：目录改名由 `setLocation` 让 qBittorrent 自己搬；库里曾有 28 个死链
种子，就是早先用文件系统搬目录留下的。正向的 `_op_rename_show_dir` 守住了这一条
（有种子 setLocation 失败就中止，不再 `_merge_tree`），但两条反方向的路径没有：

- **回退**（`_apply_undo` 的 `rename_show_dir`）：`setLocation` 的异常被
  `except Exception: continue` 吞掉，接着把新目录顶层的每一项用 `shutil.move` 搬回旧目录——
  连同那个没搬成的种子的文件；改名之后才落进新目录的种子（比如此后抓的新集）不在
  `torrent_savepaths` 里，它们的文件同样被文件系统搬走。回退还记 `reverted`、`failed=0`。
  生产上有 78 条已执行的 rename_show_dir，回退其中较早的任意一条都会命中第二种情形。
- **repair**（`repair_split_dirs`）：同样吞掉 `setLocation` 的异常再 `_merge_tree`；
  而且只给 save_path 在旧目录下的种子发 setLocation，content 在旧目录下、save_path
  在更上层的种子（Original 布局、根目录就叫剧名）直接被文件系统合并走。

qBit 不在时这两条已由 `qbit_blocker` 整体拒绝（critic N3），这里测的是 qBit 在线的情形。
"""
from __future__ import annotations

import json
import shutil

import pytest

from harness.library import DirBuilder
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding


def _dir_rename(lib, old: str, new: str) -> Finding:
    p = lib.path(old)
    return Finding(rule="title-drift", kind="title_drift", severity="important",
                   summary=f"目录名 `{old}` 与 TMDB 官方标题 `{new}` 不一致", show=old,
                   path=str(p), action=Action(op="rename_show_dir",
                                              args={"path": str(p), "new_name": new}))


def _forward(lib, old="旧名", new="新名") -> str:
    """真跑一次正向目录改名，回退用的就是它自己写下的审计记录。"""
    rep = lib.apply([_dir_rename(lib, old, new)])
    [rec] = rep.applied
    assert rec["op"] == "rename_show_dir" and not lib.path(old).exists()
    return rec["run_id"]


def _alive(lib, handle) -> bool:
    """种子声明的每个文件都真实存在（没有失联）。"""
    return all(p.exists() for p in handle.current_paths())


# ------------------------------------------------------------------ 回退
def test_sanity_rollback_moves_torrents_back_via_qbit_and_leftovers_by_file(lib):
    show = lib.show("旧名")
    t = show.season(1).single("旧名 S01E01.mkv", size=1000)
    nfo = show.local("tvshow.nfo", size=10)
    orphan_sub = show.season(1).local("旧名 S01E01.ass", size=20)   # 没有种子认领的字幕
    ident = lib.ident(t.path)
    run = _forward(lib)

    res = lib.rollback(run)

    assert res["reverted"] == 1 and res["failed"] == 0 and res["skipped"] == 0
    assert lib.qbit.torrent(t.hash)["save_path"] == str(show.path / "Season 1")
    assert _alive(lib, t) and lib.ident(t.current_paths()[0]) == ident
    assert nfo.exists() and orphan_sub.exists()        # Season 1 里的残留也逐个搬回了
    assert not lib.path("新名").exists()


def test_rollback_with_a_failed_setlocation_moves_nothing_and_is_not_reverted(lib):
    """触发一：回退时 setLocation 超时。以前吞掉异常、再把文件用 shutil.move 搬走。"""
    s1 = lib.show("旧名").season(1)
    t = s1.single("旧名 S01E01.mkv", size=1000)
    run = _forward(lib)
    lib.qbit.fail("set_location", hash=t.hash)
    before = lib.snapshot()

    res = lib.rollback(run)

    assert res["reverted"] == 0 and res["failed"] == 1
    [d] = res["failed_detail"]
    assert "setLocation" in d["error"]
    assert lib.snapshot() == before                    # 盘上、qBit 里一样都没动
    assert _alive(lib, t)


def test_rollback_refuses_when_a_torrent_landed_in_the_dir_after_the_rename(lib):
    """触发二（不需要任何故障）：改名之后才抓进新目录的种子，不在当初的记录里。"""
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    run = _forward(lib)
    late = lib.show("新名").season(2).single("新名 S02E01.mkv", size=2000)
    before = lib.snapshot()

    res = lib.rollback(run)

    assert res["reverted"] == 0 and res["skipped"] == 1 and res["failed"] == 0
    [d] = res["skipped_detail"]
    assert "不在" in d["skip_reason"] and late.hash[:8] in d["skip_reason"]
    assert lib.snapshot() == before
    assert _alive(lib, late)


def test_dry_run_rollback_reports_the_same_refusal(lib):
    lib.show("旧名").season(1).single("旧名 S01E01.mkv", size=1000)
    run = _forward(lib)
    lib.show("新名").season(2).single("新名 S02E01.mkv", size=2000)
    before = lib.snapshot()

    res = lib.rollback(run, dry_run=True)

    assert res["skipped"] == 1 and res["reverted"] == 0
    assert lib.snapshot() == before


def test_rollback_leftover_merge_never_touches_a_path_a_torrent_still_claims(lib):
    """qBittorrent 的 setLocation 是异步的：回退发出之后文件可能还在新目录里。
    残留搬运必须绕开任何种子声明的路径，交给 qBit 自己搬完。"""
    show = lib.show("旧名")
    t = show.season(1).single("旧名 S01E01.mkv", size=1000)
    nfo = show.local("tvshow.nfo", size=10)
    ident = lib.ident(t.path)
    run = _forward(lib)
    lib.qbit.async_moves = True

    res = lib.rollback(run)
    claimed = lib.path("新名/Season 1/旧名 S01E01.mkv")
    assert res["reverted"] == 1
    assert claimed.exists() and lib.ident(claimed) == ident   # 残留搬运没碰它
    assert nfo.exists()                                       # 无主残留照常搬回

    lib.qbit.drain()                                          # qBit 搬完
    assert _alive(lib, t) and lib.ident(t.current_paths()[0]) == ident
    assert t.current_paths()[0].parent == show.path / "Season 1"


def test_rollback_skips_when_the_renamed_dir_is_gone(lib):
    """改名后的目录此后又被改过名：以前照记录把种子逐个 setLocation 回原路径，
    不管它们此刻在哪；现在如实跳过。"""
    s1 = lib.show("旧名").season(1)
    t = s1.single("旧名 S01E01.mkv", size=1000)
    run = _forward(lib)
    lib.qbit.set_location([t.hash], str(lib.path("第三个名字/Season 1")))   # 经 qBit 又搬走了
    shutil.rmtree(lib.path("新名"))                                         # 只剩空目录壳
    before = lib.snapshot()

    res = lib.rollback(run)

    assert res["skipped"] == 1 and res["reverted"] == 0
    assert "不存在" in res["skipped_detail"][0]["skip_reason"]
    assert lib.snapshot() == before
    assert _alive(lib, t)


# ------------------------------------------------------------------ repair
def _split_record(lib, run_id: str = "r2") -> None:
    """一条"改名做过、旧目录没清干净"的审计：新名 ← 旧名。"""
    rec = {"ts": "2026-09-20T00:00:00", "run_id": run_id, "status": "applied",
           "dry_run": False, "rule": "title-drift", "kind": "title_drift",
           "op": "rename_show_dir", "args": {}, "summary": "x",
           "undo": {"op": "rename_show_dir", "path": str(lib.path("新名")),
                    "new_name": "旧名"}}
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _repair(lib, run_id="r2", dry_run=False) -> dict:
    return Executor(lib.context(), dry_run=dry_run, run_id="rp").repair_split_dirs(run_id)


def test_sanity_repair_moves_live_torrents_via_qbit_and_leftovers_by_file(lib):
    old = lib.show("旧名")
    t = old.season(1).single("旧名 S01E01.mkv", size=1000)
    stray = old.season(1).local("旧名 S01E01.ass", size=20)
    lib.show("新名").season(1)
    _split_record(lib)
    ident = lib.ident(t.path)

    res = _repair(lib)

    [d] = res["detail"]
    assert d["moved_via_qbit"] == 1 and d["moved_via_fs"] == 1 and d["old_removed"]
    assert lib.qbit.torrent(t.hash)["save_path"] == str(lib.path("新名/Season 1"))
    assert _alive(lib, t) and lib.ident(t.current_paths()[0]) == ident
    assert not stray.exists() and lib.path("新名/Season 1/旧名 S01E01.ass").exists()


def test_repair_with_a_failed_setlocation_skips_the_merge_for_that_pair(lib):
    old = lib.show("旧名")
    t = old.season(1).single("旧名 S01E01.mkv", size=1000)
    old.season(1).local("旧名 S01E01.ass", size=20)
    lib.show("新名").season(1)
    _split_record(lib)
    lib.qbit.fail("set_location", hash=t.hash)
    before = lib.snapshot()

    res = _repair(lib)

    [d] = res["detail"]
    assert d.get("error") and "setLocation" in d["error"]
    assert d["moved_via_fs"] == 0 and not d["old_removed"]
    assert lib.snapshot() == before
    assert _alive(lib, t)


def test_repair_never_merges_files_a_torrent_rooted_above_the_dir_claims(lib):
    """Original 布局、save_path 是媒体根、根目录恰好叫剧名：content 在旧目录下，
    save_path 却不在。以前它不在 setLocation 名单里，文件被 _merge_tree 直接搬走。"""
    old = lib.show("旧名")
    rooted = DirBuilder(old, lib.media_root).torrent(
        {"Season 1/旧名 S01E02.mkv": 1000, "Season 1/旧名 S01E03.mkv": 1000},
        name="旧名", layout="original")
    assert rooted.view()["content_path"] == str(old.path)
    stray = old.season(1).local("tvshow.nfo", size=10)
    lib.show("新名").season(1)
    _split_record(lib)

    res = _repair(lib)

    assert _alive(lib, rooted)                        # 它的文件一个没被文件系统搬走
    assert not stray.exists()                         # 无主的照常合并
    [d] = res["detail"]
    assert not d["old_removed"]                       # 旧目录里还有种子的文件，不能清


def test_repair_dry_run_changes_nothing(lib):
    """预演以前也会真的发 setLocation（在 dry-run 判断之前）。"""
    old = lib.show("旧名")
    old.season(1).single("旧名 S01E01.mkv", size=1000)
    old.season(1).local("旧名 S01E01.ass", size=20)
    lib.show("新名").season(1)
    _split_record(lib)
    before = lib.snapshot()

    res = _repair(lib, dry_run=True)

    [d] = res["detail"]
    assert d["would_move_via_qbit"] == 1 and d["would_move_via_fs"] == 1
    assert lib.snapshot() == before
    assert not [c for c in lib.qbit.calls if c[0] == "set_location"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_repair_ignores_torrents_of_sibling_dirs_with_a_common_prefix(lib, dry_run):
    """`under()` 而不是 startswith：`旧名2` 的种子不属于 `旧名`。"""
    lib.show("旧名").season(1).local("旧名 S01E01.mkv", size=1000)
    sib = lib.show("旧名2").season(1).single("旧名2 S01E01.mkv", size=1000)
    lib.show("新名").season(1)
    _split_record(lib)

    _repair(lib, dry_run=dry_run)

    assert lib.qbit.torrent(sib.hash)["save_path"] == str(lib.path("旧名2/Season 1"))
    assert _alive(lib, sib)


def test_cmd_repair_reports_a_failed_pair_and_exits_nonzero(lib, monkeypatch, capsys):
    """一对没修成要让 launchd / 运维者看得见：❌ 加非零退出码，而不是"分裂目录 1 对"加 0。"""
    import argparse

    from media_agent import cli
    old = lib.show("旧名")
    t = old.season(1).single("旧名 S01E01.mkv", size=1000)
    lib.show("新名").season(1)
    _split_record(lib)
    lib.qbit.fail("set_location", hash=t.hash)
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())

    rc = cli.cmd_repair(argparse.Namespace(run="r2", dry_run=False), lib.cfg)

    assert rc == 1
    assert "❌" in capsys.readouterr().out
    assert _alive(lib, t)
