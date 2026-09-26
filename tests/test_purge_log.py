"""隔离区的每一次硬删除：先在 `state/purge.jsonl` 写下意图并落盘，再删，再记完成。

整改前的测绘（2026-09-26）：
- `run` 末尾的时间清理（`Executor.purge_trash`）按日期 `rmtree` 整个隔离区日目录，一行记录都不写。
  生产 run.log 里两次："清理 4 个过期文件，释放 3.0GB"、"清理 2 个过期文件，释放 1.7GB"——
  删的是哪 6 个文件，只能靠事后倒推（药屋 `[Tokuten]`、K-ON `[SP04] Cast Interview` ……）。
- `purge --apply` 先 unlink 再写日志：日志写失败就报"删除失败"，而文件其实已经没了。

这里断言：意图先于 unlink 落在盘上；进程在两步之间死掉，留下的状态下一轮能认出来、补完；
删目录只 rmdir 空目录，从不 rmtree。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta

import pytest

from media_agent import disposal


def _log(lib) -> list[dict]:
    p = lib.cfg.state_dir / disposal.LOG_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _phases(lib, path) -> list[str]:
    return [r["phase"] for r in _log(lib) if r.get("op") == "purge" and r.get("path") == str(path)]


def _trash_file(lib, rel="2026-09-14/尼古喵喵/Season 1/尼古喵喵 S01E11.mkv", data=b"x" * 64):
    p = lib.cfg.trash_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _plog(lib, run_id="p001") -> disposal.PurgeLog:
    return disposal.PurgeLog(lib.cfg.state_dir / disposal.LOG_NAME, run_id)


def test_intent_is_on_disk_before_the_unlink(lib, monkeypatch):
    victim = _trash_file(lib)
    seen = {}
    real = disposal._unlink

    def unlink(p):
        seen["phases"] = _phases(lib, victim)          # unlink 那一刻盘上的日志
        real(p)

    monkeypatch.setattr(disposal, "_unlink", unlink)

    why = disposal.hard_delete(_plog(lib), victim, 64, disposition="extras",
                               reason="到期的特典")

    assert why == ""
    assert seen["phases"] == ["intent"]
    assert not victim.exists()
    assert _phases(lib, victim) == ["intent", "done"]
    [intent] = [r for r in _log(lib) if r["phase"] == "intent"]
    assert intent["bytes"] == 64 and intent["disposition"] == "extras"
    assert intent["run_id"] == "p001" and intent["reason"] == "到期的特典"


class _Crash(BaseException):
    """模拟进程在两步之间被杀（launchd 超时、断电）：不是 Exception，谁也拦不住。"""


def test_crash_between_intent_and_unlink_is_recoverable(lib, monkeypatch):
    victim = _trash_file(lib)
    real = disposal._unlink

    def boom(p):
        raise _Crash()

    monkeypatch.setattr(disposal, "_unlink", boom)
    with pytest.raises(_Crash):
        disposal.hard_delete(_plog(lib), victim, 64, disposition="extras", reason="x")

    assert victim.exists()                              # 没删成：原件还在
    assert _phases(lib, victim) == ["intent"]           # 意图悬着

    # 只撤这一处替身：monkeypatch.undo() 会连 conftest 的隔离（PROJECT_ROOT）一起撤掉
    monkeypatch.setattr(disposal, "_unlink", real)
    rec = disposal.recover(_plog(lib, "p002"))

    assert [r["path"] for r in rec] == [str(victim)]
    assert _phases(lib, victim) == ["intent", "abandoned"]
    assert victim.exists()                              # 恢复只补记录，不替上一轮删
    assert disposal.pending(lib.cfg.state_dir / disposal.LOG_NAME) == []


def test_crash_after_unlink_is_completed_on_recovery(lib, monkeypatch):
    victim = _trash_file(lib)
    real_done = disposal.PurgeLog.done

    def boom(self, *a, **k):
        raise _Crash()

    monkeypatch.setattr(disposal.PurgeLog, "done", boom)
    with pytest.raises(_Crash):
        disposal.hard_delete(_plog(lib), victim, 64, disposition="extras", reason="x")
    monkeypatch.setattr(disposal.PurgeLog, "done", real_done)

    assert not victim.exists()
    assert _phases(lib, victim) == ["intent"]

    disposal.recover(_plog(lib, "p002"))

    assert _phases(lib, victim) == ["intent", "done"]
    [done] = [r for r in _log(lib) if r["phase"] == "done"]
    assert done["recovered"] is True and done["run_id"] == "p002"


def test_unlink_failure_is_recorded_and_reported(lib, monkeypatch):
    victim = _trash_file(lib)

    def denied(p):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(disposal, "_unlink", denied)
    why = disposal.hard_delete(_plog(lib), victim, 64, disposition="extras", reason="x")

    assert "Permission" in why
    assert victim.exists()
    assert _phases(lib, victim) == ["intent", "failed"]


def test_log_write_failure_means_nothing_is_deleted(lib, monkeypatch):
    """意图写不下去（盘满）就不删：没有记录的硬删除一次都不许发生。"""
    victim = _trash_file(lib)

    def full(self, rec):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(disposal.PurgeLog, "_append", full)
    why = disposal.hard_delete(_plog(lib), victim, 64, disposition="extras", reason="x")

    assert "No space" in why
    assert victim.exists()


@pytest.mark.parametrize("kind", ["dir", "symlink"])
def test_only_regular_files_are_ever_unlinked(lib, kind):
    target = _trash_file(lib, "2026-09-14/a/b.mkv")
    odd = lib.cfg.trash_dir / "2026-09-14" / "odd"
    if kind == "dir":
        odd.mkdir()
        (odd / "inner.mkv").write_bytes(b"keep")
    else:
        odd.symlink_to(target)

    why = disposal.hard_delete(_plog(lib), odd, 0, disposition="other", reason="x")

    assert why and "普通文件" in why
    assert odd.exists() or odd.is_symlink()
    assert target.exists()
    assert _phases(lib, odd) == []                      # 连意图都不写


def test_sweep_removes_only_empty_dirs_and_never_rmtree(lib, monkeypatch):
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("隔离区里不许 rmtree"))
    keep = _trash_file(lib, "2026-09-14/尼古喵喵/Season 1/尼古喵喵 10【TV版】.mp4")
    empty = lib.cfg.trash_dir / "2026-09-15" / "朱音落语" / "Season 1"
    empty.mkdir(parents=True)

    n = disposal.sweep_empty_dirs(lib.cfg.trash_dir)

    assert n == 3                                       # Season 1、朱音落语、2026-09-15
    assert keep.exists()
    assert not (lib.cfg.trash_dir / "2026-09-15").exists()
    assert lib.cfg.trash_dir.is_dir()                   # 隔离区根目录本身不删


# ------------------------------------------------------------------ purge --apply
def test_cmd_purge_apply_writes_the_intent_before_deleting(lib, monkeypatch):
    """手动 `purge --apply` 同样先落意图：以前先 unlink 再写日志，日志写失败就报成"删除失败"。"""
    import argparse

    from media_agent import cli

    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.show("尼古喵喵").season(1).single("尼古喵喵 S01E01.mkv")   # qBit 里得有种子，扫描才可信
    ts = datetime.now() - timedelta(days=40)                  # 过了 30 天保留期的特典
    victim = _trash_file(lib, f"{ts:%Y-%m-%d}/尼古喵喵/Season 1/尼古喵喵 [Tokuten][01].mkv")
    rec = {"ts": ts.isoformat(timespec="seconds"), "run_id": "old", "status": "applied",
           "dry_run": False, "rule": "extras-in-library", "kind": "extra", "op": "trash",
           "args": {"path": str(lib.media_root / "尼古喵喵/Season 1/尼古喵喵 [Tokuten][01].mkv")},
           "summary": "特典", "trashed_to": str(victim)}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    order = []
    real = disposal._unlink

    def unlink(p):
        order.append(_phases(lib, victim))
        real(p)

    monkeypatch.setattr(disposal, "_unlink", unlink)
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("隔离区里不许 rmtree"))

    args = argparse.Namespace(apply=True, verbose=False, no_tmdb=True)
    assert cli.cmd_purge(args, lib.cfg) == 0

    assert order == [["intent"]]
    assert _phases(lib, victim) == ["intent", "done"]
    assert not victim.exists()
    assert not (lib.cfg.trash_dir / f"{ts:%Y-%m-%d}").exists()  # 空目录逐层 rmdir
    assert os.path.isdir(lib.cfg.trash_dir)
