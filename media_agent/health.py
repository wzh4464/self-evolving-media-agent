"""运行健康：跨轮的种子数基线（本节），以及每轮的健康报告（后续小节）。都在 `state/health/` 下。

## 种子数合理性（第 1 阶段遗留）

`torrents()` 成功返回、却比上一轮少了一大截：接口上没有任何报错，后果却与 qBittorrent 不可用一样——
缺了种子的文件全成了"纯本地文件"，改名走文件系统、隔离跳过种子（LAT-01 的形态）。触发是一个只恢复了一部分
会话的 qBit（容器重建时 BT_backup 丢了一部分）。第 1 阶段只挡住了"0 个种子"（`scan`，不需要跨轮状态）。

判据：`上一轮的数 − 此刻的数 − 审计里记着的本项目摘除数 > max(TORRENT_DROP_MIN, ⌈上一轮 × TORRENT_DROP_PCT%⌉)`
就把这次扫描当作读不全（`state.qbit_errors`）：执行器整批拒绝、`run` 退出码 3、大声说（与第 1 阶段同一条路）。

- **"上一轮" = 最近一次被采信的 `run` 的计数**（`torrent-count.json`，扫描之前那一刻的时间）。被拒绝的那一轮
  不挪基线——否则第二轮就拿残缺的数当基准放行了。
- **审计里的摘除**：基线时刻之后、非预演的、真摘掉了种子记录的审计记录（`torrent_removals`）。本项目自己
  批量摘死种（drop_torrent）不会让下一轮误判。
- **人为的批量删除**（在 qBit 里手动删）审计里没有：`media-agent health --accept-torrent-count` 按此刻 qBittorrent
  的数重置基线。它会一直拦到有人确认——宁可停几轮（每轮都大声说、退出码 3），也不拿残缺视图动手。
- 基线文件坏了 / 没有：当作没有基线（第一次部署就是这样），这一轮之后重新记。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import audit as auditlog

HEALTH_DIR = "health"
BASELINE_NAME = "torrent-count.json"


def health_dir(state_dir) -> Path:
    return Path(state_dir) / HEALTH_DIR


# ---------------------------------------------------------------------------
# 种子数基线
# ---------------------------------------------------------------------------
def load_baseline(state_dir) -> dict | None:
    """`{count, run_id, ts, source}`；没有、读不了、格式不对都返回 None（当作没有基线）。"""
    p = health_dir(state_dir) / BASELINE_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("count"), int):
        return None
    return data


def save_baseline(state_dir, *, count: int, run_id: str, ts: str, source: str) -> str:
    """记下被采信的种子数。`ts` 是**扫描之前**那一刻：之后的摘除都算进下一轮的"解释得通"。
    `source`：`run`（一轮正常的 run）或 `accepted`（人确认的）。永不抛异常，返回问题（空串 = 写好了）。"""
    d = health_dir(state_dir)
    p = d / BASELINE_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{BASELINE_NAME}.tmp"
        tmp.write_text(json.dumps({"count": int(count), "run_id": run_id, "ts": ts,
                                   "source": source}, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, p)
        return ""
    except Exception as e:                          # noqa: BLE001 —— 观测写不进去不拦正事
        return f"种子数基线写不进 {p}（{type(e).__name__}: {e}）"


def _removed_torrent(rec: dict) -> bool:
    """这条审计记录是否**摘掉了**一条种子记录（生产审计的各种形状，见 `actions`）。"""
    status = rec.get("status")
    if status == auditlog.APPLIED:
        if rec.get("op") == "drop_torrent" or rec.get("dropped"):
            return True
        return bool((rec.get("undo") or {}).get("torrent_record_lost"))
    if status in (auditlog.FAILED, auditlog.UNKNOWN):
        # 搬文件失败、但种子那一步已经做了；或未确认、但发出过删除
        return (rec.get("torrent_record_lost") is True
                or "qbit.delete" in (rec.get("effects_attempted") or []))
    return False


def torrent_removals(audit_log, since: str) -> int:
    """`since`（ISO 时间，含）之后审计里记着的、本项目摘掉的种子记录数（按 hash 去重，非预演）。"""
    seen: set[str] = set()
    n = 0
    for rec in auditlog.iter_records(audit_log):
        if rec.get("dry_run") or str(rec.get("ts") or "") < since or not _removed_torrent(rec):
            continue
        h = str((rec.get("args") or {}).get("torrent_hash") or "").lower()
        if h:
            if h in seen:
                continue
            seen.add(h)
        n += 1
    return n


def torrent_count_problem(cfg, count: int) -> str:
    """此刻 `count` 个种子与上一轮被采信的基线相比是否说不通；说得通返回空串，否则返回原因。"""
    base = load_baseline(cfg.state_dir)
    if not base:
        return ""
    prev = base["count"]
    drop = prev - count
    allowed = max(int(cfg.torrent_drop_min), math.ceil(prev * float(cfg.torrent_drop_pct) / 100))
    if drop <= allowed:
        return ""
    removed = torrent_removals(cfg.audit_log, str(base.get("ts") or ""))
    if drop - removed <= allowed:
        return ""
    return (f"qBittorrent 报告 {count} 个种子，上一轮（{base.get('run_id', '?')}）是 {prev} 个，少了 {drop} 个，"
            f"审计里记着的本项目摘除只有 {removed} 个（阈值 {allowed}）——多半是 qBit 的会话没恢复完整"
            f"（容器重建丢了一部分 BT_backup）。确认是人为删除的：media-agent health --accept-torrent-count")
