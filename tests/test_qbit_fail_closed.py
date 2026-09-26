"""qBittorrent 不可用或读不全时，这一轮必须**什么都不改**（fail closed）。

critic N2 / N3 / LAT-01：
- `build_context` 登录失败只打一行警告，`ctx.qbit=None` 照样执行。扫描看到 0 个种子，
  每个有种子的文件都成了"纯本地文件"：改名走被禁止的文件系统分支（AGENTS.md
  第 3 条），隔离跳过种子处理。生产实例：20260919T225410（qBit 登录超时）把
  `朱音落语/Season 1/朱音落语 S01E12.mp4` 移进隔离区，`torrent_hash ""`——
  它其实归种子 d08f05a7；下一轮 20260920T170126 又把那个种子的记录删了。
- `scan._torrent_files` 把任何 `files()` 错误缓存成 `[]`。一次 WebUI 超时（每轮约
  539 次调用）就让那个种子的文件变成无主文件，同样的两条禁路全开，而且无声无息。
- `rollback` / `repair` / `purge --apply` 也不看 qBit 在不在：逆改名退化成
  `Path.rename`，`repair` 用 `_merge_tree` 搬活种子的文件，purge 丢掉种子证据。
"""
from __future__ import annotations

import argparse
import json

import pytest

from harness import video
from media_agent import cli
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding

AKANE_RAW = "[Group] Akane-banashi - 12 [1080p].mp4"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _akane(lib):
    """朱音落语 E12：种子里的已改名文件 + 一份更好的本地副本 + 一个待改名的新集。"""
    s1 = lib.show("朱音落语").season(1)
    owned = s1.single("朱音落语 S01E12.mp4", size=508_000_000, name=AKANE_RAW,
                      probe=video("h264"))
    s1.local("[Better] Akane-banashi - 12 [1080p].mkv", size=900_000_000, probe=TWO_SUBS)
    fresh = s1.single("[Group] Akane-banashi - 13 [1080p].mp4", size=500_000_000)
    return s1, owned, fresh


def test_sanity_with_qbit_up_the_scene_does_produce_actions(lib):
    """对照组：qBit 在时这个现场确实会产生改名与隔离，下面的"什么都没改"才有意义。"""
    _akane(lib)
    c = lib.cycle()
    assert c.applied("rename") and c.applied("trash")


def test_qbit_down_run_changes_nothing(lib):
    _akane(lib)
    lib.qbit_down()
    before = lib.snapshot()

    c = lib.cycle()

    assert c.actions()                                   # 诊断照常出结论……
    assert c.report.refused and "qBittorrent" in c.report.refused
    assert not (c.report.applied or c.report.skipped or c.report.failed)
    assert lib.snapshot() == before                      # ……但一个字节都不改
    assert lib.audit() == []


@pytest.mark.allow("log_failure", match=r"files\(")
def test_one_files_timeout_fails_the_whole_run_closed(lib):
    s1, owned, fresh = _akane(lib)
    lib.qbit.fail("files", hash=fresh.hash)              # 扫描时这一个种子超时
    before = lib.snapshot()

    c = lib.cycle()

    assert c.state.qbit_errors and fresh.hash[:8] in c.state.qbit_errors[0]
    assert c.report.refused
    assert lib.snapshot() == before
    assert lib.qbit.file_names(fresh.hash) == ["[Group] Akane-banashi - 13 [1080p].mp4"]


@pytest.mark.allow("log_failure", match=r"torrents\(")
def test_torrents_failure_fails_closed_instead_of_crashing(lib):
    _akane(lib)
    lib.qbit.fail("torrents")
    before = lib.snapshot()

    c = lib.cycle()

    assert c.state.qbit_errors and c.report.refused
    assert lib.snapshot() == before


def _empty_session(lib, monkeypatch):
    """qBit 登录正常、`torrents()` 成功，却一个种子都不报（端着空会话的 qBit）。"""
    monkeypatch.setattr(lib.qbit, "torrents", lambda category=None: [])


@pytest.mark.allow("log_failure", match="不可信")
def test_empty_torrent_list_with_a_populated_library_fails_closed(lib, monkeypatch):
    """审查复现：以前 refused 为空，`[Group] Akane-banashi - 13` 经文件系统改名成
    `朱音落语 S01E13.mp4`，qBit 仍声明着原名——种子失联（LAT-01 的形态，且没有任何报错）。"""
    s1, owned, fresh = _akane(lib)
    _empty_session(lib, monkeypatch)
    before = lib.snapshot()

    c = lib.cycle()

    assert c.report.refused and "0 个种子" in c.report.refused
    assert lib.snapshot() == before
    assert lib.qbit.file_names(fresh.hash) == ["[Group] Akane-banashi - 13 [1080p].mp4"]
    assert fresh.path.exists()


def test_empty_torrent_list_is_fine_for_an_empty_library(lib, monkeypatch):
    lib.show("空番").season(1)
    _empty_session(lib, monkeypatch)

    c = lib.cycle()

    assert not c.state.qbit_errors and not c.report.refused


def test_empty_torrent_list_override_for_a_torrentless_library(lib, monkeypatch):
    """库里确实一个种子都不用：`QBIT_ALLOW_EMPTY=1` 放行。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("测试番").season(1)
    s1.local("测试番 S01E01.mkv", size=1000)
    _empty_session(lib, monkeypatch)

    c = lib.cycle()

    assert not c.state.qbit_errors and not c.report.refused


def test_qbit_allow_empty_env_is_parsed(monkeypatch):
    from media_agent.config import load_config
    assert load_config().qbit_allow_empty is False
    monkeypatch.setenv("QBIT_ALLOW_EMPTY", "1")
    assert load_config().qbit_allow_empty is True


def test_rollback_refuses_without_qbit(lib):
    _akane(lib)
    c = lib.cycle()
    assert c.applied("rename") and c.applied("trash")
    after = lib.snapshot()
    lib.qbit_down()

    res = lib.rollback(c.run_id)

    assert res["refused"] and res["reverted"] == 0
    assert lib.snapshot() == after


def test_undo_rename_never_falls_back_to_filesystem(lib):
    """种子里找不到该文件时，逆改名以前会退化成 `Path.rename`。"""
    s1 = lib.show("测试番").season(1)
    t = s1.single("测试番 S01E01.mkv", name="[G] Show - 01.mkv")
    stray = s1.local("测试番 S01E02.mkv", size=1000)
    rec = {"ts": "2026-09-20T00:00:00", "run_id": "r1", "status": "applied",
           "dry_run": False, "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename",
           "args": {}, "summary": "x",
           "undo": {"op": "rename", "path": str(stray), "new_name": "[G] Show - 02.mkv",
                    "torrent_hash": t.hash}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    before = lib.snapshot()

    res = lib.rollback("r1")

    assert res["skipped"] == 1 and res["reverted"] == 0
    assert lib.snapshot() == before


def test_rename_op_never_falls_back_to_filesystem_without_qbit(lib):
    """纵深防御：即使绕过 apply 的总闸，带 hash 的改名也不许走文件系统。"""
    s1 = lib.show("测试番").season(1)
    t = s1.single("[G] Show - 01.mkv")
    f = Finding(rule="unrenamed-file", kind="unrenamed", severity="important",
                summary="x", show="测试番", path=str(t.path), torrent_hash=t.hash,
                action=Action(op="rename", args={"path": str(t.path),
                                                 "new_name": "测试番 S01E01.mkv",
                                                 "torrent_hash": t.hash}))
    ex = Executor(lib.context(qbit=None), dry_run=False, run_id="x")

    ex._dispatch(f, f.action)

    assert t.path.exists() and not (s1.path / "测试番 S01E01.mkv").exists()
    assert [r["op"] for r in ex.report.skipped] == ["rename"]


def test_repair_refuses_without_qbit(lib):
    old = lib.show("旧名").season(1)
    old.local("旧名 S01E01.mkv", size=1000)
    new = lib.show("新名").season(1)
    rec = {"ts": "2026-09-20T00:00:00", "run_id": "r2", "status": "applied",
           "dry_run": False, "rule": "title-drift", "kind": "title_drift",
           "op": "rename_show_dir", "args": {}, "summary": "x",
           "undo": {"op": "rename_show_dir", "path": str(new.show.path), "new_name": "旧名"}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    lib.qbit_down()
    before = lib.disk()

    res = Executor(lib.context(), dry_run=False, run_id="rp").repair_split_dirs("r2")

    assert res["refused"] and res["pairs"] == 0
    assert lib.disk() == before


# ------------------------------------------------------------------ CLI 出口
@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0,
                kind=None, show=None, limit=None, run=None, last=False,
                apply=False, verbose=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_run_exits_nonzero_and_skips_everything_when_qbit_down(offline_cli, capsys):
    lib = offline_cli
    _akane(lib)
    stale = lib.cfg.trash_dir / "2020-01-01" / "x.mkv"   # 远超保留期的隔离区内容
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    lib.qbit_down()
    before = lib.snapshot()

    rc = cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr()
    assert rc == cli.EXIT_DEGRADED != 0
    assert "拒绝" in out.out and "qBittorrent" in out.out
    assert lib.snapshot() == before
    assert stale.exists()                                 # 时间清理也不跑


def test_cmd_apply_exits_nonzero_when_qbit_down(offline_cli, capsys):
    lib = offline_cli
    _akane(lib)
    lib.qbit_down()
    before = lib.snapshot()

    rc = cli.cmd_apply(_args(), lib.cfg)

    assert rc == cli.EXIT_DEGRADED
    assert "拒绝" in capsys.readouterr().out
    assert lib.snapshot() == before


def test_cmd_rollback_exits_nonzero_when_qbit_down(offline_cli, capsys):
    lib = offline_cli
    _akane(lib)
    c = lib.cycle()
    lib.qbit_down()
    after = lib.snapshot()

    rc = cli.cmd_rollback(_args(run=c.run_id), lib.cfg)

    assert rc == cli.EXIT_DEGRADED
    assert lib.snapshot() == after


def test_cmd_repair_exits_nonzero_when_qbit_down(offline_cli, capsys):
    """以前 CLI 层没有测试：删掉 `if res.get("refused")` 这一句，repair 就打印
    「分裂目录 0 对」并返回 0——正是 launchd 上看不见的"降级还 rc 0"。"""
    lib = offline_cli
    old = lib.show("旧名").season(1)
    old.local("旧名 S01E01.mkv", size=1000)
    new = lib.show("新名").season(1)
    rec = {"ts": "2026-09-20T00:00:00", "run_id": "r2", "status": "applied",
           "dry_run": False, "rule": "title-drift", "kind": "title_drift",
           "op": "rename_show_dir", "args": {}, "summary": "x",
           "undo": {"op": "rename_show_dir", "path": str(new.show.path), "new_name": "旧名"}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    lib.qbit_down()
    before = lib.disk()

    rc = cli.cmd_repair(_args(run="r2"), lib.cfg)

    out = capsys.readouterr().out
    assert rc == cli.EXIT_DEGRADED
    assert "拒绝" in out and "分裂目录" not in out
    assert lib.disk() == before


def test_cmd_purge_apply_refuses_when_qbit_down(offline_cli, capsys):
    lib = offline_cli
    victim = lib.cfg.trash_dir / "2026-09-20" / "测试番" / "Season 1" / "测试番 S01E01.mkv"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"trashed")
    lib.qbit_down()

    rc = cli.cmd_purge(_args(apply=True), lib.cfg)

    assert rc == cli.EXIT_DEGRADED
    assert victim.read_bytes() == b"trashed"
    assert "拒绝" in capsys.readouterr().out


@pytest.mark.allow("log_failure", match=r"torrents\(")
def test_cmd_run_skips_evolve_when_the_rescan_is_degraded(offline_cli, capsys, monkeypatch):
    """apply 之后的演进重扫若读不全，演进器会把有种子的文件当成盲区去立永久规则。"""
    lib = offline_cli
    lib.configure(evolve_mode="propose")               # 默认冻结；这里要走演进分支
    lib.llm.script({"rules": []})                      # 打开 FakeLLM，演进分支才会走
    calls = {"n": 0}
    real = lib.qbit.torrents

    def flaky(category=None):
        calls["n"] += 1
        if calls["n"] >= 2:                            # 第一次扫描正常，重扫时超时
            raise TimeoutError("timed out (injected)")
        return real(category)

    monkeypatch.setattr(lib.qbit, "torrents", flaky)
    from media_agent import evolution
    monkeypatch.setattr(evolution.Evolver, "evolve",
                        lambda *a, **k: pytest.fail("残缺快照不该进演进器"))

    rc = cli.cmd_run(_args(), lib.cfg)

    assert rc == cli.EXIT_DEGRADED
    assert "演进：跳过" in capsys.readouterr().out


def test_cmd_evolve_refuses_on_a_degraded_snapshot(offline_cli, monkeypatch):
    lib = offline_cli
    lib.configure(evolve_mode="propose")
    lib.llm.script({"rules": []})
    lib.qbit_down()
    from media_agent import evolution
    monkeypatch.setattr(evolution.Evolver, "evolve",
                        lambda *a, **k: pytest.fail("残缺快照不该进演进器"))

    assert cli.cmd_evolve(_args(), lib.cfg) == cli.EXIT_DEGRADED
