"""隔离区处置：硬删除的唯一出口。

隔离区（`state/trash/`）是本项目里"删除"的可回退形态；从这里再删一次就没有下一层保险了。
所以这里的每一次 unlink 都要能事后回答三件事：删了什么、凭什么、删成了没有。

**预写日志（write-ahead）**：`state/purge.jsonl`（与会话里手工处置的记录同一份文件）。

    {"op": "purge", "phase": "intent", "id": "<run_id>#<n>", "path", "bytes", "disposition", "reason", …}
    → fsync → unlink →
    {"op": "purge", "phase": "done", "id", …}         # 或 "failed"（带 error）

为什么先写后删：整改前的测绘（2026-09-26）里，`run` 末尾的时间清理按日期 `rmtree` 整个隔离区
日目录、一行记录都不写——生产 run.log 只留下"清理 4 个过期文件，释放 3.0GB""清理 2 个过期文件，
释放 1.7GB"，删的是哪 6 个文件只能倒推；`purge --apply` 则先 unlink 再写日志，日志写失败就报成
"删除失败"，而文件其实已经没了。先写意图、落盘之后再删：

- 意图写不下去（盘满）→ 不删。没有记录的硬删除一次都不许发生。
- 进程死在意图与 unlink 之间 → 文件还在，下一轮 `recover` 把那条意图记成 `abandoned`，
  文件照常重新评估。
- 进程死在 unlink 与 done 之间 → 文件已不在，下一轮 `recover` 补一条 `done`（`recovered: true`）。

**只删普通文件、逐个删**；删空了的目录逐层 `rmdir`，**从不 `rmtree`**——整目录删除会把目录里
混进来的、此刻不该删的东西（同一日目录里被关口留下的、人手放进来的）一起带走。
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

LOG_NAME = "purge.jsonl"
OP = "purge"
_OPEN = "intent"
_CLOSED = ("done", "failed", "abandoned")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _unlink(p: Path) -> None:
    """唯一的 unlink 出口（测试在这里模拟进程中断）。"""
    os.unlink(p)


class PurgeLog:
    """`state/purge.jsonl` 上的预写日志。一次处置（`run` 的一轮、一次 `purge --apply`）一个实例。"""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._n = 0

    def _append(self, rec: dict) -> None:
        # 每条都 flush + fsync：意图必须在 unlink 之前真的落在盘上，而不是还在页缓存里
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def intent(self, path: Path, size: int, **facts) -> str:
        self._n += 1
        pid = f"{self.run_id}#{self._n}"
        self._append({"op": OP, "phase": _OPEN, "id": pid, "run_id": self.run_id, "ts": _now(),
                      "path": str(path), "bytes": int(size), **facts})
        return pid

    def done(self, pid: str, path: Path, **extra) -> None:
        self._append({"op": OP, "phase": "done", "id": pid, "run_id": self.run_id,
                      "ts": _now(), "path": str(path), **extra})

    def failed(self, pid: str, path: Path, error: str) -> None:
        self._append({"op": OP, "phase": "failed", "id": pid, "run_id": self.run_id,
                      "ts": _now(), "path": str(path), "error": error})

    def abandoned(self, pid: str, path: Path, note: str) -> None:
        self._append({"op": OP, "phase": "abandoned", "id": pid, "run_id": self.run_id,
                      "ts": _now(), "path": str(path), "note": note})


def pending(log_path: Path) -> list[dict]:
    """写了意图、却既没有 done 也没有 failed / abandoned 的记录（上一次处置中途被打断）。"""
    if not Path(log_path).exists():
        return []
    opened: dict[str, dict] = {}
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict) or r.get("op") != OP or not r.get("id"):
            continue
        if r.get("phase") == _OPEN:
            opened[r["id"]] = r
        elif r.get("phase") in _CLOSED:
            opened.pop(r["id"], None)
    return list(opened.values())


def recover(log: PurgeLog) -> list[dict]:
    """把上一次中断留下的悬空意图补完，返回它们。只补记录，**不替上一轮删任何东西**：
    文件已不在 → `done`（`recovered: true`）；还在 → `abandoned`，这一轮照常重新评估它。"""
    out = pending(log.path)
    for r in out:
        p = Path(r["path"])
        if os.path.lexists(p):
            log.abandoned(r["id"], p, "意图写下之后没删成（进程中断）：文件还在，重新评估")
        else:
            log.done(r["id"], p, recovered=True)
    return out


def hard_delete(log: PurgeLog, path: Path, size: int, **facts) -> str:
    """硬删除隔离区里的一个普通文件：意图 → unlink → done。删掉了返回空串，否则返回原因。

    `facts` 原样写进意图（`disposition` / `reason` / `rule` / `origin` / `slot` / …），
    日后回答"凭什么删"。不是普通文件（目录、符号链接）一律不删、连意图都不写。
    """
    path = Path(path)
    try:
        st = os.lstat(path)
    except OSError as e:
        return f"读不到隔离文件：{type(e).__name__}: {e}"
    if not stat.S_ISREG(st.st_mode):
        return f"不是普通文件（目录或符号链接），隔离区只逐个删普通文件：{path}"
    try:
        pid = log.intent(path, size, **facts)
    except OSError as e:
        return f"删除意图写不进 {log.path.name}，不删：{type(e).__name__}: {e}"
    try:
        _unlink(path)
    except OSError as e:
        why = f"{type(e).__name__}: {e}"
        try:
            log.failed(pid, path, why)
        except OSError:
            pass                          # 意图还悬着：下一轮 recover 看到文件还在，记 abandoned
        return why
    try:
        log.done(pid, path)
    except OSError:
        pass                              # 文件已删、意图在盘上：下一轮 recover 补 done
    return ""


def sweep_empty_dirs(root: Path) -> int:
    """隔离区里删空了的目录，自底向上逐个 `rmdir`（非空的 rmdir 会失败，正好不动）。
    `root` 本身不删。返回删掉的目录数。**从不 `rmtree`**。"""
    root = Path(root)
    n = 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()),
                    key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
            n += 1
        except OSError:
            continue
    return n
