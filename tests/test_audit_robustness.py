"""写审计出错不能冲出执行器，也不能让这一条记录消失（B1，critic N8 的余项）。

隔离区与媒体在同一个 APFS 容器里，约 94% 满（critic N8）：真到磁盘满的那一轮，最先写不进去的
就是 `state/audit.jsonl`。以前 `_audit` 直接 `open("a").write(json.dumps(...))`：

- 写不进去（ENOSPC、权限）抛 OSError。改动本身已经做了，这一条却没有记录；
- 这个异常被 `apply()` 的 `except` 接住，再去写一条 failed——又抛，这次冲出 `apply()`，整轮中止，
  `run` 末尾的隔离区处置（磁盘满时唯一能腾空间的一步）跟着被跳过；
- `args` / `extra` 里混进一个 `Path` 之类序列化不了的值，`json.dumps` 抛 TypeError，同样的连锁。

现在：审计先进本轮报告（内存），再写盘；写不进 audit.jsonl 就原样转写到 stderr 与
`state/audit.fallback.jsonl`（尽力而为），计数、在 run 的输出与退出码里大声说。回退、`runs`、
隔离区处置、失败模式统计读审计时两个文件一起读——转写的记录照样能回退。
"""
from __future__ import annotations

import argparse
import errno
import json
from pathlib import Path

import pytest
from harness import video

from media_agent import audit as audit_mod
from media_agent import cli
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding

LOLI = "[LoliHouse] Yani Neko - {:02d} [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _two_renames(lib):
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    b = s1.single(LOLI.format(9), size=593_601_177, probe=SUBS)
    return s1, a, b


def _disk_full(monkeypatch, lib, *, times: int | None = 1, fallback_too: bool = False):
    """往 audit.jsonl（以及可选的 audit.fallback.jsonl）追加时报 ENOSPC。"""
    real = audit_mod.append_line
    left = {"n": times}
    targets = {Path(lib.cfg.audit_log)}
    if fallback_too:
        targets.add(audit_mod.fallback_path(lib.cfg.audit_log))

    def append(path, line):
        if Path(path) in targets and (left["n"] is None or left["n"] > 0):
            if left["n"] is not None:
                left["n"] -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, line)

    monkeypatch.setattr(audit_mod, "append_line", append)


def _crash_on(monkeypatch, torrent_hash: str) -> None:
    """这个种子的改名动作一进来就抛异常（任何改动发出之前）：走 `apply()` 的兜底，记 failed。"""
    real = Executor._op_rename

    def flaky(self, f, a):
        if a.args.get("torrent_hash") == torrent_hash:
            raise RuntimeError("bug before any write (injected)")
        return real(self, f, a)

    monkeypatch.setattr(Executor, "_op_rename", flaky)


def _fallback_records(lib) -> list[dict]:
    p = audit_mod.fallback_path(lib.cfg.audit_log)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.allow("audit_fallback")
def test_disk_full_audit_goes_to_stderr_and_fallback_and_the_batch_goes_on(lib, monkeypatch,
                                                                          capsys):
    _, a, b = _two_renames(lib)
    _disk_full(monkeypatch, lib, times=1)

    c = lib.cycle()

    # 两个改名都做了、都在本轮报告里（第一条的审计没写进 audit.jsonl）
    assert sorted(r["args"]["torrent_hash"] for r in c.applied("rename")) == sorted([a.hash, b.hash])
    [problem] = c.report.audit_problems
    assert "No space left" in problem
    # 本批次第一条记录（`seq` 是它在本批次里的序号）写盘失败
    everything = c.report.applied + c.report.skipped + c.report.failed
    [first] = [r for r in everything if r["seq"] == 1]
    # 原样转写：stderr 一份、audit.fallback.jsonl 一份
    err = capsys.readouterr().err
    assert json.dumps(first, ensure_ascii=False) in err
    [fb] = _fallback_records(lib)
    assert fb == first
    assert first not in lib.audit()                     # 主审计里确实没有它……
    assert len(lib.audit(c.run_id)) == len(everything) - 1
    assert "审计" in c.report.summary() and "1 条" in c.report.summary()


@pytest.mark.allow("audit_fallback")
def test_fallback_record_is_still_rolled_back(lib, monkeypatch):
    """……但回退、`runs` 读审计时两个文件一起读：转写的那一条照样还原。"""
    _two_renames(lib)
    _disk_full(monkeypatch, lib, times=1)
    c = lib.cycle()
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176,
                          "尼古喵喵/Season 1/尼古喵喵 S01E09.mkv": 593_601_177}

    [run] = [r for r in Executor(lib.context()).list_runs() if r["run_id"] == c.run_id]
    assert run["applied"] == len(c.report.applied) and run["undoable"] >= 2
    res = lib.rollback(c.run_id)

    assert res["reverted"] == run["undoable"] and not res["failed"]
    assert lib.disk() == {f"尼古喵喵/Season 1/{LOLI.format(8)}": 593_601_176,
                          f"尼古喵喵/Season 1/{LOLI.format(9)}": 593_601_177}


@pytest.mark.allow("audit_fallback")
@pytest.mark.allow("failed_record", match="bug before any write")
def test_audit_failure_inside_the_failure_handler_does_not_escape_apply(lib, monkeypatch, capsys):
    """critic N8 的余项：动作抛异常 → `apply` 写 failed → 写审计又抛 → 以前冲出 `apply()`。"""
    _, a, b = _two_renames(lib)
    findings = lib.diagnose()
    _crash_on(monkeypatch, a.hash)
    _disk_full(monkeypatch, lib, times=None)                         # 主审计一直写不进

    rep = Executor(lib.context(), dry_run=False, run_id="t-full").apply(findings)

    assert [r["args"]["torrent_hash"] for r in rep.failed] == [a.hash]
    assert [r["args"]["torrent_hash"] for r in rep.applied if r["op"] == "rename"] == [b.hash]
    assert len(rep.audit_problems) == len(rep.applied) + len(rep.skipped) + len(rep.failed)
    assert {r["run_id"] for r in _fallback_records(lib)} == {"t-full"}
    assert len(_fallback_records(lib)) == len(rep.audit_problems)
    assert not lib.cfg.audit_log.exists() or not lib.audit("t-full")


@pytest.mark.allow("audit_fallback")
def test_fallback_file_also_unwritable_still_reaches_stderr(lib, monkeypatch, capsys):
    _two_renames(lib)
    _disk_full(monkeypatch, lib, times=None, fallback_too=True)

    c = lib.cycle()

    assert len(c.applied("rename")) == 2
    err = capsys.readouterr().err
    for rec in c.report.applied:
        assert json.dumps(rec, ensure_ascii=False) in err
    assert "audit.fallback.jsonl" in err                              # 连备用文件也写不进，也说出来
    assert not _fallback_records(lib)


@pytest.mark.allow("audit_fallback")
def test_unserializable_value_is_written_degraded_not_lost(lib):
    """`args` 里混进一个 `Path`：以前 `json.dumps` 抛 TypeError——改名已经做了、记录却没有，
    接着 failed 那一条同样序列化不了，冲出 `apply()`。现在按字符串写进主审计、标明降级，照样能回退。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    f = Finding(rule="unrenamed-file", kind="unrenamed", severity="minor", summary="改名",
                show="尼古喵喵", path=str(t.path), torrent_hash=t.hash,
                action=Action(op="rename", args={"path": t.path,            # ← Path，不是 str
                                                 "new_name": "尼古喵喵 S01E08.mkv",
                                                 "torrent_hash": t.hash}))

    rep = Executor(lib.context(), dry_run=False, run_id="t-path").apply([f])

    assert len(rep.applied) == 1
    assert rep.audit_problems and "序列化" in rep.audit_problems[0]
    [line] = lib.audit("t-path")
    assert line["args"]["path"] == str(t.path) and line["audit_degraded"]
    assert lib.rollback("t-path")["reverted"] == 1
    assert lib.qbit.file_names(t.hash) == [LOLI.format(8)]


def test_torn_last_line_does_not_swallow_the_next_record(lib):
    """上一个进程写到一半就死了（磁盘满、被杀），audit.jsonl 末尾是半行、没有换行。以前下一条
    直接接在后面，粘成一行坏 JSON，读的人连它一起跳过——那一条的回退就此找不到。"""
    _two_renames(lib)
    lib.cfg.audit_log.write_text('{"ts": "2026-09-26T03:00:00", "run_id": "dead", "sta',
                                 encoding="utf-8")

    c = lib.cycle()

    [run] = [r for r in Executor(lib.context()).list_runs() if r["run_id"] == c.run_id]
    assert run["applied"] == len(c.report.applied) > 0
    assert lib.rollback(c.run_id)["reverted"] == run["undoable"] > 0


# ------------------------------------------------------------------ run：不中止、跑到隔离区处置、非零退出
def _args(**kw):
    base = {"dry_run": False, "no_tmdb": True, "no_evolve": False, "max_proposals": 0}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.allow("audit_fallback")
@pytest.mark.allow("failed_record", match="bug before any write")
def test_cmd_run_reaches_disposal_and_exits_nonzero_when_audit_is_lost(lib, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    _, a, _ = _two_renames(lib)
    _crash_on(monkeypatch, a.hash)
    _disk_full(monkeypatch, lib, times=None)
    disposed = []
    real = cli.disposal.dispose

    def spy(*args, **kw):
        disposed.append(kw.get("mode"))
        return real(*args, **kw)

    monkeypatch.setattr(cli.disposal, "dispose", spy)

    rc = cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr()
    assert disposed == ["run"]                            # 以前异常冲出 apply，这一步被跳过
    assert rc == cli.EXIT_AUDIT_INCOMPLETE != 0
    assert "审计" in out.out and "audit.fallback.jsonl" in out.out
    assert "审计" in out.err
