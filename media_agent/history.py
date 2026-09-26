"""发现历史：每轮 `run`（与 `diagnose`）的**全部**发现落 `state/findings/<run_id>.jsonl`，带稳定指纹。

**为什么**（critic N11）：`run` 以前只把发现当文字打进 run.log。有动作的还能从 audit.jsonl 里看出
"同一个改名每轮都被跳过"，没有动作的——`pending_ownership`、`seal_conflict`、`season_layout_mismatch`、
抓取的"找不到番组页"——一过这一轮就只剩日志里的一行字。"这个问题已经挂了三天没人管"没有任何地方
能回答。卡住检测（`stuck`）要的正是这个：同一个问题在连续几轮里都在。

**指纹不能用摘要。** `Finding.key()` 对没有路径的发现退回 `(show, summary)`，而摘要里嵌着计数：
「S01 缺 3 集（已播 12 集……）」下一轮是「缺 2 集」，「竟有 4 个文件声称是同一集」多一个文件就变了
（subscription.py / grab.py 的摘要都是这种）。拿它当身份，每轮都是"新问题"，永远不会被认作卡住。

指纹 = sha1(规则, 类型, 目标) 的前 16 位十六进制，目标按这个顺序取第一个有的：

1. `show#subject`：检测器声明的"这是关于哪一集 / 哪一季"（`Finding.subject`，如 `S01E08` / `S02`）。
   集位级的发现（封存冲突、所有权未交接、幻影、集号解析异常）的 `path` 只是桶里第一个文件，谁排第一会变；
2. 路径；3. `torrent:<hash>`（小写）；4. show。

**文件格式**：第一行 header（`type=header`：run_id、ts、cmd、degraded、findings 条数），其后每条发现一行
（`type=finding`：fp、rule、kind、severity、show、target、path、torrent_hash、subject、summary、op、classified）。
一轮什么都没发现也写 header——"连续几轮都有"要靠空的那一轮来断。整份先写临时文件再改名，读的一方
永远看不到写了一半的快照。**写永不抛异常**（返回问题列表，由调用方大声说）：发现历史是观测，不能因为
它写不进去就中止一轮。只保留最近 `KEEP_RUNS` 份。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

FINDINGS_DIR = "findings"
# 保留多少份快照（按文件名 = 批次 ID 排序，批次 ID 的前 15 位是时间戳）。6 小时一轮是 15 天；
# 卡住检测只要最近 STUCK_RUNS（默认 4）份，多留的给人翻。
KEEP_RUNS = 60


def target_of(f) -> str:
    """指纹里的"目标"：这条发现说的是哪个东西（见模块文档的取值顺序）。"""
    subject = str(getattr(f, "subject", "") or "")
    if subject:
        return f"{f.show}#{subject}"
    if f.path:
        return str(f.path)
    if f.torrent_hash:
        return f"torrent:{str(f.torrent_hash).lower()}"
    return str(f.show or "")


def fingerprint(f) -> str:
    """稳定指纹：同一个规则、同一类问题、同一个目标，跨轮、跨进程都相同。**不含摘要。**
    `.agents/acks.json` 存的就是它——改算法等于让所有确认失效，必须有意为之。"""
    raw = "\x1f".join((str(f.rule), str(f.kind), target_of(f)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def record_of(f) -> dict:
    """一条发现在快照里的样子。"""
    return {"type": "finding", "fp": fingerprint(f), "rule": f.rule, "kind": f.kind,
            "severity": f.severity, "show": f.show, "target": target_of(f),
            "path": str(f.path or ""), "torrent_hash": f.torrent_hash,
            "subject": getattr(f, "subject", "") or "", "summary": f.summary,
            "op": f.action.op if f.action else None, "classified": bool(f.classified)}


def findings_dir(state_dir) -> Path:
    return Path(state_dir) / FINDINGS_DIR


def write_snapshot(state_dir, run_id: str, findings, *, cmd: str, degraded: bool = False,
                   now: datetime | None = None) -> tuple[Path | None, list[str]]:
    """把这一轮的全部发现写成 `state/findings/<run_id>.jsonl`，并只留最近 `KEEP_RUNS` 份。

    返回 `(文件路径 | None, 问题列表)`。**永不抛异常**。"""
    problems: list[str] = []
    d = findings_dir(state_dir)
    path = d / f"{run_id}.jsonl"
    try:
        head = {"type": "header", "run_id": run_id,
                "ts": (now or datetime.now()).isoformat(timespec="seconds"),
                "cmd": cmd, "degraded": bool(degraded), "findings": len(findings)}
        lines = [json.dumps(head, ensure_ascii=False)]
        lines += [json.dumps(record_of(f), ensure_ascii=False, default=str) for f in findings]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{run_id}.jsonl.tmp"
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:                          # noqa: BLE001 —— 观测写不进去不能中止这一轮
        return None, [f"发现历史写不进 {path}（{type(e).__name__}: {e}）"]
    problems += prune(d, KEEP_RUNS)
    return path, problems


def prune(d: Path, keep: int) -> list[str]:
    """只留按文件名排序的最后 `keep` 份 `.jsonl`。删不掉的说出来，不抛。"""
    try:
        snaps = sorted(p for p in d.iterdir() if p.suffix == ".jsonl" and not p.name.startswith("."))
    except OSError as e:
        return [f"列不出 {d}（{type(e).__name__}: {e}），没有清理旧快照"]
    out = []
    for p in snaps[:max(len(snaps) - keep, 0)]:
        try:
            p.unlink()
        except OSError as e:
            out.append(f"删不掉旧快照 {p.name}（{type(e).__name__}: {e}）")
    return out


@dataclass
class Snapshot:
    run_id: str
    ts: str
    cmd: str
    degraded: bool
    findings: list[dict] = field(default_factory=list)

    @property
    def fps(self) -> set[str]:
        return {r.get("fp") for r in self.findings if r.get("fp")}


def load_snapshots(state_dir) -> list[Snapshot]:
    """全部快照，按文件名（批次 ID）排序。没有 header 的文件、坏行跳过；读不了的文件当作没有。"""
    d = findings_dir(state_dir)
    try:
        paths = sorted(p for p in d.iterdir() if p.suffix == ".jsonl" and not p.name.startswith("."))
    except OSError:
        return []
    out: list[Snapshot] = []
    for p in paths:
        try:
            raw = p.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue                                # 读不了就当作这一轮没有记录：卡住检测据此断开
        head, recs = None, []
        for line in raw:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("type") == "header" and head is None:
                head = rec
            elif rec.get("type") == "finding":
                recs.append(rec)
        if head is None:
            continue
        out.append(Snapshot(run_id=str(head.get("run_id") or p.stem), ts=str(head.get("ts") or ""),
                            cmd=str(head.get("cmd") or ""), degraded=bool(head.get("degraded")),
                            findings=recs))
    return out


# ---------------------------------------------------------------------------
# 卡住检测
# ---------------------------------------------------------------------------
# 只有这些严重度、或带动作的发现才会被认作"卡住"：minor 的（「均在 7 天内播出，等发布即可」）挂多久都正常。
STUCK_SEVERITIES = ("critical", "important")


def qualifies(rec: dict) -> bool:
    """这条快照里的发现算不算"该收敛却没收敛"的那一类：带动作，或严重度 ≥ important。"""
    return bool(rec.get("op")) or rec.get("severity") in STUCK_SEVERITIES


@dataclass
class Stuck:
    """一个连续 `runs` 轮都在的问题。`ack` 是 `.agents/acks.json` 里仍有效的确认（没有为 None）。"""
    fp: str
    rule: str
    kind: str
    severity: str
    show: str
    target: str
    summary: str
    op: str | None
    runs: int
    first_seen: str
    ack: dict | None = None

    def to_dict(self) -> dict:
        return {"fp": self.fp, "rule": self.rule, "kind": self.kind, "severity": self.severity,
                "show": self.show, "target": self.target, "summary": self.summary, "op": self.op,
                "runs": self.runs, "first_seen": self.first_seen, "ack": self.ack}


def find_stuck(state_dir, run_id: str, *, min_runs: int, acks: dict | None = None,
               today=None) -> list[Stuck]:
    """本轮（`run_id` 的快照）里、连续至少 `min_runs` 轮 `run` 都在的问题，按连续轮数从多到少。

    - 只数 `cmd == "run"` 的快照：手动 diagnose 几次不该让它提前升级，也不打断；
    - 读 qBittorrent 不完整（`degraded`）的一轮不算数也不打断——那一轮的发现不可信；本轮就是降级的，
      返回空（残缺快照里"不在了"不代表解决了，"还在"也不可信）；
    - 连续 = 按批次 ID 排好的 run 快照里一轮不缺；中间哪一轮没有它（解决过又复发）就从那之后重新数。
    - 确认过（`acks`，`until` 含当天）的照样返回，`ack` 字段带上确认，由调用方决定只计数不列出。
    """
    runs = [s for s in load_snapshots(state_dir) if s.cmd == "run" and not s.degraded]
    idx = next((i for i, s in enumerate(runs) if s.run_id == run_id), None)
    if idx is None:
        return []
    today = today or datetime.now().date()
    history_ = runs[:idx + 1]
    current = {r["fp"]: r for r in history_[-1].findings if r.get("fp") and qualifies(r)}
    # 每份快照里"算数"的指纹先收成集合：逐条比会是 发现数² × 轮数，库里一乱（几千条）就是上亿次比较
    present = [{r.get("fp") for r in snap.findings if qualifies(r)} for snap in history_]
    out: list[Stuck] = []
    for fp, rec in current.items():
        streak, first = 0, history_[-1]
        for snap, fps in zip(reversed(history_), reversed(present)):
            if fp not in fps:
                break
            streak, first = streak + 1, snap
        if streak < min_runs:
            continue
        out.append(Stuck(fp=fp, rule=str(rec.get("rule") or ""), kind=str(rec.get("kind") or ""),
                         severity=str(rec.get("severity") or ""), show=str(rec.get("show") or ""),
                         target=str(rec.get("target") or ""), summary=str(rec.get("summary") or ""),
                         op=rec.get("op"), runs=streak, first_seen=first.ts,
                         ack=active_ack(acks or {}, fp, today)))
    out.sort(key=lambda s: (-s.runs, s.show, s.kind, s.fp))
    return out


def seen_fingerprints(state_dir) -> dict[str, dict]:
    """发现历史里出现过的每个指纹 → 最近一次的那条记录（`ack` 命令用来核对、补全说明）。"""
    out: dict[str, dict] = {}
    for snap in load_snapshots(state_dir):
        for r in snap.findings:
            if r.get("fp"):
                out[r["fp"]] = r
    return out


# ---------------------------------------------------------------------------
# 确认：`.agents/acks.json`（版本化的用户意图）
# ---------------------------------------------------------------------------
# 形如 {指纹: {"reason": 为什么先不管, "until"?: "YYYY-MM-DD"（含当天，过了重新提醒）,
#            "added": "YYYY-MM-DD", "what": "规则 / 类型 / 目标（给人看的）"}}。
# 为什么放 `.agents/` 而不是 `state/`：它和 `preferences.json` 一样是**人的决定**，生产行为要能从 git
# 完整复现（CHANGELOG「版本与发布约定」）；在生产上改了，部署的漂移闸门会拦下它，要带回来提交。
ACKS_NAME = "acks.json"


def acks_path() -> Path:
    from . import config
    return config.PROJECT_ROOT / ".agents" / ACKS_NAME


def load_acks(path: Path | None = None) -> tuple[dict, list[str]]:
    """读确认文件。不存在 = 没有确认；读不了 / 格式不对返回空并说明——宁可多提醒，不静默吞掉。"""
    p = Path(path or acks_path())
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeDecodeError) as e:
        return {}, [f"读不了 {p.name}（{type(e).__name__}: {e}），这一轮按没有确认处理"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {}, [f"{p.name} 不是合法 JSON（{e}），这一轮按没有确认处理"]
    if not isinstance(data, dict):
        return {}, [f"{p.name} 顶层应是 {{指纹: {{reason, until?}}}}，这一轮按没有确认处理"]
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}, []


def active_ack(acks: dict, fp: str, today) -> dict | None:
    """`fp` 此刻仍有效的确认；`until`（YYYY-MM-DD，含当天）过了、或写错了都算无效。"""
    ack = acks.get(fp)
    if not ack:
        return None
    until = ack.get("until")
    if until:
        try:
            if today > date.fromisoformat(str(until)):
                return None
        except ValueError:
            return None
    return ack


def save_acks(acks: dict, path: Path | None = None) -> Path:
    """按指纹排序、缩进 2 写回（git diff 干净）。先写临时文件再改名。"""
    p = Path(path or acks_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(dict(sorted(acks.items())), ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)
    return p
