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

**删什么**由 `purge.build_pool` 按处置类别判（见那里的模块文档），**什么时候删**由模式定：

- `run`（每轮自动）：只删判据通过**且**已过 `TRASH_RETENTION_DAYS` 的——特典 / 死种半成品到期即删，
  判重要证明得了替代者；合并发布的另一版本 / 手动 / 其它 / 没有记录的永不自动删，过了保留期在输出里
  逐个报给人。保留期内的一律不动：回退（`restore_from_trash`）要用它们，生产上从隔离区捞回来的
  最晚隔了 19 天（义妹生活 S01E01-10）。
- `manual`（`purge --apply`，人要的）：判据通过的全删——证明安全的判重不必等满保留期。

**容量闸**（`MIN_FREE_GB`，`run` 模式）：隔离区与媒体在同一个 APFS 容器（critic N8，约 94% 满），隔离
不腾空间，只有硬删除腾。媒体卷（`statvfs(MEDIA_ROOT)`）剩余低于阈值时，大声告警，并在**已证明可删**
（判据通过、满了最短隔离期）却还在保留期里的里面，从最老的开始提前删，删到回到阈值以上就停。
证明不了的、要人定的、没到期的特典一个都不为空间删。释放量按删掉的字节数估算，不每删一个就重测：
同一容器里的 APFS 本地快照会让删掉的块暂不释放，重测只会越删越多；少删的下一轮重测再补。
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG_NAME = "purge.jsonl"
OP = "purge"
_OPEN = "intent"
_CLOSED = ("done", "failed", "abandoned")


def free_bytes(path) -> int | None:
    """`path` 所在卷此刻可用的字节数（`statvfs`：f_bavail × f_frsize）；读不到返回 None。"""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


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


# ------------------------------------------------------------------ 处置
MODES = ("run", "manual")


@dataclass
class DisposalReport:
    """一次处置的结果。`dry_run` 时 `deleted` 是"会删的"。"""
    run_id: str
    mode: str
    dry_run: bool
    pool: list = field(default_factory=list)          # purge.Candidate，隔离区里的每一份
    deleted: list = field(default_factory=list)       # 删掉的（预演：会删的）
    failed: list = field(default_factory=list)        # [(Candidate, 原因)]
    changed: list = field(default_factory=list)       # [(Candidate, 原因)]：评估之后前提变了，这次不删
    recovered: list = field(default_factory=list)     # 上次中断、这次补完的意图
    refused: str = ""
    free_before: int | None = None                    # 处置前媒体卷的剩余字节（读不到为 None）
    min_free: int = 0                                 # MIN_FREE_GB 折成字节
    early: list = field(default_factory=list)         # 其中因空间不足提前删的（也在 deleted 里）

    @property
    def low_space(self) -> bool:
        return self.free_before is not None and self.free_before < self.min_free

    @property
    def free_after(self) -> int | None:
        """按删掉的字节数估算的处置后剩余（APFS 快照可能让实际释放更少）。"""
        return None if self.free_before is None else self.free_before + self.freed_bytes

    @property
    def freed_bytes(self) -> int:
        return sum(c.size for c in self.deleted)

    @property
    def overdue(self) -> list:
        """过了保留期却没删的（需要人看的、证明不了的、这次删失败的）。"""
        gone = {id(c) for c in self.deleted}
        return [c for c in self.pool if c.expired and id(c) not in gone]


def select(pool: list, mode: str, *, low_space: bool = False) -> list:
    """按模式挑出这次要删的 `[(Candidate, 是否因空间不足提前删)]`，最早隔离的在前。

    `run` 在空间不足时把"已证明可删、还在保留期里"的也排进来（在已到期的之后、同样按隔离时间）；
    删到回到阈值以上就停由调用方按实际删掉的字节数决定。"""
    if mode == "manual":
        return [(c, False) for c in sorted((c for c in pool if c.eligible),
                                           key=lambda c: c.trashed_at)]
    due = sorted((c for c in pool if c.eligible and c.expired), key=lambda c: c.trashed_at)
    early = []
    if low_space:
        early = sorted((c for c in pool if c.eligible and not c.expired),
                       key=lambda c: c.trashed_at)
    return [(c, False) for c in due] + [(c, True) for c in early]


def dispose(ctx, *, mode: str, run_id: str, dry_run: bool = False,
            now: datetime | None = None) -> DisposalReport:
    """处置隔离区：评估每一份（`purge.build_pool`），按模式挑出来，逐个预写日志后删。"""
    from . import purge

    assert mode in MODES, mode
    cfg = ctx.config
    rep = DisposalReport(run_id, mode, dry_run)
    if not dry_run and ctx.qbit is None:
        # 纵深防御：run 在 qBittorrent 不可用时整轮拒绝、根本走不到这里；purge --apply 也先拒绝了
        rep.refused = "qBittorrent 不可用：拿不到种子证据，不做不可逆删除"
        return rep
    log = PurgeLog(Path(cfg.state_dir) / LOG_NAME, run_id)
    if not dry_run:
        rep.recovered = recover(log)
    rep.min_free = int(float(cfg.min_free_gb) * 1e9)
    rep.free_before = free_bytes(cfg.media_root)
    rep.pool = purge.build_pool(ctx, now=now)
    for c, early in select(rep.pool, mode, low_space=(mode == "run" and rep.low_space)):
        if early:
            if rep.free_after >= rep.min_free:
                break                             # 回到阈值以上就停：保留期内的尽量留给回退
            reason = (f"空间不足（媒体卷剩 {rep.free_after / 1e9:.1f} GB < MIN_FREE_GB="
                      f"{cfg.min_free_gb:g}），提前删已证明安全的：{c.why}")
        else:
            reason = c.why
        if dry_run:
            rep.deleted.append(c)
            if early:
                rep.early.append(c)
            continue
        # 评估到这里可能隔着几分钟：按此刻再问一遍（purge.recheck），前提变了就不删、下一轮重新评估
        why = purge.recheck(ctx, c)
        if why:
            rep.changed.append((c, why))
            continue
        why = hard_delete(
            log, c.trash_path, c.size, mode="capacity" if early else mode,
            disposition=c.disposition, rule=c.rule,
            origin=c.origin, slot=list(c.slot) if c.slot else None,
            survivor=str(c.survivor) if c.survivor else None,
            trashed_at=c.trashed_at.isoformat(timespec="seconds"),
            age_days=round(c.age_days or 0, 2), reason=reason)
        if why:
            rep.failed.append((c, why))
        else:
            rep.deleted.append(c)
            if early:
                rep.early.append(c)
    if not dry_run:
        sweep_empty_dirs(cfg.trash_dir)
    return rep
