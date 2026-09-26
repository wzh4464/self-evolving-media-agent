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
from datetime import datetime
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


# ---------------------------------------------------------------------------
# 每轮健康报告
# ---------------------------------------------------------------------------
# 报告只留最近这么多份（与发现历史同一口径：6 小时一轮是 15 天）
KEEP_REPORTS = 60

# 退出码（与 cli 的常量同值；这里是"哪种 critical 对应哪个退出码"的唯一定义）
EXIT_CRASH = 1                # 异常冲出了这一轮（Python 未捕获异常本来也是 1）
EXIT_DEGRADED = 3             # 整批拒绝改动（qBittorrent 不可用 / 读不全 / 种子数骤降）
EXIT_AUDIT_INCOMPLETE = 4     # 有审计没能原样写进 audit.jsonl
EXIT_CRITICAL = 5             # 其余 critical（处置之后媒体卷剩余仍低于 MIN_FREE_GB）
# 几种 critical 同时出现时取靠前的：越靠前越"这一轮说明不了别的"
_PRECEDENCE = (EXIT_CRASH, EXIT_DEGRADED, EXIT_AUDIT_INCOMPLETE, EXIT_CRITICAL)
_CRITICAL_EXIT = {"crash": EXIT_CRASH, "refused": EXIT_DEGRADED,
                  "rescan_degraded": EXIT_DEGRADED, "audit_incomplete": EXIT_AUDIT_INCOMPLETE,
                  "disk_full": EXIT_CRITICAL}
STATUSES = ("ok", "warn", "critical")
ICON = {"ok": "✅", "warn": "⚠️", "critical": "🔴"}


def exit_code_for(critical_codes, rc: int) -> int:
    """`run` 的退出码：这一轮已经决定的 `rc`（0 / 3 / 4）与 critical 原因各自对应的退出码里，取最靠前的。"""
    codes = {_CRITICAL_EXIT.get(c, EXIT_CRITICAL) for c in critical_codes}
    if rc:
        codes.add(rc)
    for c in _PRECEDENCE:
        if c in codes:
            return c
    return rc


def trash_usage(trash_dir) -> tuple[int, int]:
    """隔离区里文件的总字节数与个数（`lstat`，不跟软链）。读不了的跳过，永不抛。"""
    total = n = 0
    for root, _dirs, files in os.walk(trash_dir, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
                n += 1
            except OSError:
                continue
    return total, n


def grab_stats(findings, report) -> dict:
    """抓取这一轮：提议几集、加上几集、其中 409（已经有了）几集、元数据超时几集、失败 / 未确认 / 跳过几集。"""
    ops = lambda recs: [r for r in recs if r.get("op") == "grab_episode"]  # noqa: E731
    applied = ops(report.applied)
    return {"proposed": sum(1 for f in findings if f.action and f.action.op == "grab_episode"),
            "applied": len(applied),
            "already_present": sum(1 for r in applied if r.get("already_present") is True),
            "metadata_timeouts": sum(1 for r in applied
                                     if (r.get("metadata") or {}).get("outcome") == "timeout"),
            "failed": len(ops(report.failed)), "unknown": len(ops(report.unknown)),
            "skipped": len(ops(report.skipped))}


def unrenamed_old(findings, report, torrents: list, threshold_hours: float,
                  now: float | None = None) -> list[dict]:
    """这一轮之后仍是发布名、而且已经待了超过 `threshold_hours` 的文件（`unrenamed-file` 提了改名、这一轮没改成）。

    "待了多久"：有种子的按种子加入的时间（下载中也该改名——拿到种子就改）；纯本地文件按 ctime（搬进来、
    改过名都会更新它，mtime 却可能是原文件的老时间）。2026-09-03「最新三集刮削失败」：文件在发布名上躺了六小时。
    """
    import time
    now = now or time.time()
    done = {str((r.get("args") or {}).get("path")) for r in report.applied if r.get("op") == "rename"}
    added = {str(t.get("hash") or "").lower(): t.get("added_on") for t in torrents}
    out = []
    for f in findings:
        if f.kind != "unrenamed" or not f.action or f.action.op != "rename" or f.path in done:
            continue
        since = added.get(str(f.torrent_hash or "").lower()) if f.torrent_hash else None
        if not since:
            try:
                since = os.stat(f.path).st_ctime
            except OSError:
                continue
        hours = (now - float(since)) / 3600
        if hours > threshold_hours:
            out.append({"path": str(f.path), "show": f.show, "hours": round(hours, 1),
                        "summary": f.summary[:120]})
    out.sort(key=lambda u: -u["hours"])
    return out


class RunHealth:
    """一轮 `run` 的健康信息，边跑边记，收尾时 `finish(rc)` 定状态与退出码。

    各阶段调用对应的方法；异常冲出时前面记下的照样在，没走到的阶段在报告里就是 None（"没跑到"，
    不是"没问题"）。"""

    def __init__(self, cfg, *, run_id: str, cmd: str = "run", dry_run: bool = False):
        import time
        self.cfg = cfg
        self._t0 = time.monotonic()
        self.data: dict = {
            "version": 1, "run_id": run_id, "cmd": cmd, "dry_run": bool(dry_run),
            "started": datetime.now().isoformat(timespec="seconds"), "finished": None,
            "duration_s": None, "status": None, "exit_code": None, "reasons": [],
            "clients": None, "degraded": {"refused": "", "qbit_errors": [], "rescan": ""},
            "crash": None, "paused": "", "locked": "", "detectors": None, "findings": None, "actions": None,
            "grab": None, "stuck": None, "unrenamed": None, "trash": None, "torrents": None,
            "evolve": None, "logged_errors": {"count": 0, "by_tag": {}, "samples": []},
        }

    # ---- 各阶段 ----
    def tap(self, log):
        """包一层 `ctx.log`：数"被吞掉的错误"那种日志行（含「失败」/「出错」），按行首的 `[标签]` 归类。"""
        import re
        tag_re = re.compile(r"^\s*\[([^\]]{1,40})\]")
        le = self.data["logged_errors"]

        def logged(msg, *a, **k):
            text = str(msg)
            if "失败" in text or "出错" in text:
                m = tag_re.match(text)
                tag = m.group(1) if m else "-"
                le["count"] += 1
                le["by_tag"][tag] = le["by_tag"].get(tag, 0) + 1
                if len(le["samples"]) < 10:
                    le["samples"].append(text[:200])
            return log(msg, *a, **k)

        return logged

    def clients(self, ctx) -> None:
        st = dict(getattr(ctx, "client_status", None) or {})
        st.setdefault("qbit", "ok" if ctx.qbit is not None else "down")
        st.setdefault("ab", "ok" if ctx.ab is not None else "off")
        st.setdefault("abdb", "ok" if ctx.abdb is not None else "off")
        st.setdefault("tmdb", "ok" if (ctx.tmdb is not None and ctx.tmdb.enabled) else "off")
        st.setdefault("llm", "ok" if (ctx.llm is not None and ctx.llm.enabled) else "off")
        self.data["clients"] = st

    def scanned(self, state, prev_baseline: dict | None) -> None:
        self.data["degraded"]["qbit_errors"] = list(state.qbit_errors)
        listed = getattr(state, "qbit_listed", False)
        self.data["torrents"] = {"current": len(state.torrents) if listed else None,
                                 "previous": (prev_baseline or {}).get("count"),
                                 "previous_run": (prev_baseline or {}).get("run_id")}

    def diagnosed(self, reg, findings) -> None:
        by_sev: dict = {}
        for f in findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        self.data["detectors"] = {"count": len(reg.detectors), "errors": list(reg.errors),
                                  "load_errors": list(getattr(reg, "load_errors", []))}
        self.data["findings"] = {"total": len(findings),
                                 "actionable": sum(1 for f in findings if f.action),
                                 "by_severity": by_sev}

    def refused(self, why: str) -> None:
        self.data["degraded"]["refused"] = why

    def applied(self, report, findings, state) -> None:
        def brief(recs):
            return [{"op": r.get("op"), "rule": r.get("rule"), "summary": str(r.get("summary"))[:100],
                     "error": str(r.get("error") or r.get("reason") or "")[:200]} for r in recs[:10]]
        self.data["actions"] = {
            "applied": len(report.applied), "skipped": len(report.skipped),
            "failed": len(report.failed), "unknown": len(report.unknown),
            "audit_problems": len(report.audit_problems),
            "failed_detail": brief(report.failed), "unknown_detail": brief(report.unknown),
            "audit_problem_detail": [str(p)[:200] for p in report.audit_problems[:10]]}
        self.data["grab"] = grab_stats(findings, report)
        old = unrenamed_old(findings, report, state.torrents, self.cfg.unrenamed_alert_hours)
        self.data["unrenamed"] = {"threshold_hours": self.cfg.unrenamed_alert_hours,
                                  "count": len(old), "old": old[:50]}

    def rescan_degraded(self, why: str) -> None:
        self.data["degraded"]["rescan"] = why

    def evolve(self, what: str) -> None:
        self.data["evolve"] = what

    def disposed(self, rep) -> None:
        after = rep.free_after
        self.data["trash"] = {
            "free_bytes": rep.free_before, "free_after_bytes": after,
            "min_free_bytes": int(rep.min_free), "low_space": bool(rep.low_space),
            "space_short": after is not None and after < rep.min_free,
            "disposal": {"refused": rep.refused, "deleted": len(rep.deleted),
                         "freed_bytes": rep.freed_bytes, "early": len(rep.early),
                         "failed": len(rep.failed), "changed": len(rep.changed),
                         "overdue": len(rep.overdue), "dry_run": rep.dry_run}}

    def stuck(self, stuck: list) -> None:
        self.data["stuck"] = {"open": [s.to_dict() for s in stuck if not s.ack],
                              "acked": sum(1 for s in stuck if s.ack)}

    def paused(self, why: str) -> None:
        self.data["paused"] = why

    def locked(self, holder: str) -> None:
        self.data["locked"] = holder

    def crashed(self, e: BaseException) -> None:
        from .kernel import _where
        try:
            err = f"{type(e).__name__}: {e}"
        except Exception:                           # noqa: BLE001 —— str(e) 本身出错也要记下类型
            err = type(e).__name__
        self.data["crash"] = {"error": err, "where": _where(e, depth=4)}

    # ---- 收尾 ----
    def reasons(self) -> list[dict]:
        d, out = self.data, []

        def add(level, code, text):
            out.append({"level": level, "code": code, "text": text})

        if d["crash"]:
            add("critical", "crash", f"本轮崩溃：{d['crash']['error']}（{d['crash']['where']}）")
        if d["degraded"]["refused"]:
            add("critical", "refused", f"整批拒绝改动：{d['degraded']['refused']}")
        if d["degraded"]["rescan"]:
            add("critical", "rescan_degraded", f"修复之后重扫时 qBittorrent 读不全：{d['degraded']['rescan']}")
        act = d["actions"] or {}
        if act.get("audit_problems"):
            add("critical", "audit_incomplete",
                f"{act['audit_problems']} 条审计没能原样写进 audit.jsonl（已转写 stderr 与备用文件）")
        trash = d["trash"] or {}
        gb = 1e9
        if trash.get("space_short"):
            add("critical", "disk_full",
                f"处置之后媒体卷估计仍只剩 {trash['free_after_bytes'] / gb:.1f} GB，低于 MIN_FREE_GB "
                f"{trash['min_free_bytes'] / gb:.0f} GB，证明得了可删的都删了——要人腾空间")
        if d["paused"]:
            add("warn", "paused", f"维护暂停：{d['paused']}")
        if d["locked"]:
            add("warn", "locked", f"运行锁被占着（{d['locked']}），这一轮什么都没做")
        det = d["detectors"] or {}
        if det.get("errors"):
            rules = "、".join(dict.fromkeys(e["rule"] for e in det["errors"]))
            add("warn", "detector_crash", f"{len(det['errors'])} 条规则崩了（{rules}），这一轮的诊断不完整")
        if det.get("load_errors"):
            add("warn", "rule_load_failed", f"{len(det['load_errors'])} 个演进规则文件加载失败、没挂上")
        if act.get("failed"):
            add("warn", "failed_actions", f"{act['failed']} 个动作失败（没生效）")
        if act.get("unknown"):
            add("warn", "unknown_actions", f"{act['unknown']} 个动作未确认（也许生效了，要人核对）")
        st = d["stuck"] or {}
        if st.get("open"):
            add("warn", "stuck", f"{len(st['open'])} 个问题连续 ≥{self.cfg.stuck_runs} 轮都在")
        un = d["unrenamed"] or {}
        if un.get("count"):
            add("warn", "unrenamed_old",
                f"{un['count']} 个文件超过 {un['threshold_hours']:g} 小时还是发布名（刮削器认不出）")
        cl = d["clients"] or {}
        if str(cl.get("ab", "")).startswith("down"):
            add("warn", "ab_down", f"AutoBangumi 连不上（{cl['ab']}），订阅修复这一轮做不了")
        if cl and cl.get("tmdb") != "ok":
            add("warn", "tmdb_off", "TMDB 不可用（未配置 TMDB_API_KEY），标题对齐与抓取相关规则跳过")
        disp = trash.get("disposal")
        if disp is not None:                        # 处置跑到了（拒绝 / 崩溃的轮次没有这些数）
            if trash.get("low_space") and not trash.get("space_short"):
                add("warn", "low_space", "媒体卷剩余曾低于 MIN_FREE_GB，已从证明可删的里面提前删回阈值以上")
            if trash.get("free_bytes") is None and not disp["refused"]:
                add("warn", "free_space_unknown", "读不到媒体卷的剩余空间，容量闸这一轮不起作用")
            if disp.get("failed"):
                add("warn", "disposal_failed", f"隔离区 {disp['failed']} 个文件硬删除失败")
        return out

    def finish(self, rc: int) -> dict:
        import time
        d = self.data
        d["finished"] = datetime.now().isoformat(timespec="seconds")
        d["duration_s"] = round(time.monotonic() - self._t0, 1)
        total, n = trash_usage(self.cfg.trash_dir)
        d["trash"] = {**(d["trash"] or {}), "bytes": total, "files": n}
        d["trash"].setdefault("min_free_bytes", int(float(self.cfg.min_free_gb) * 1e9))
        d["reasons"] = self.reasons()
        levels = {r["level"] for r in d["reasons"]}
        d["status"] = "critical" if "critical" in levels else "warn" if levels else "ok"
        d["exit_code"] = exit_code_for({r["code"] for r in d["reasons"] if r["level"] == "critical"},
                                       rc)
        return d


def write_report(state_dir, report: dict) -> tuple[Path | None, list[str]]:
    """写 `state/health/<run_id>.json`，只留最近 `KEEP_REPORTS` 份。永不抛异常。"""
    d = health_dir(state_dir)
    path = d / f"{report.get('run_id')}.json"
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{path.name}.tmp"
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:                          # noqa: BLE001 —— 观测写不进去不拦正事
        return None, [f"健康报告写不进 {path}（{type(e).__name__}: {e}）"]
    return path, _prune_reports(d)


def _reports(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir()
                  if p.suffix == ".json" and p.name != BASELINE_NAME and not p.name.startswith("."))


def _prune_reports(d: Path) -> list[str]:
    try:
        reports = _reports(d)
    except OSError as e:
        return [f"列不出 {d}（{type(e).__name__}: {e}），没有清理旧报告"]
    out = []
    for p in reports[:max(len(reports) - KEEP_REPORTS, 0)]:
        try:
            p.unlink()
        except OSError as e:
            out.append(f"删不掉旧报告 {p.name}（{type(e).__name__}: {e}）")
    return out


def load_report(state_dir, run_id: str | None = None) -> dict | None:
    """某一轮（默认最近一轮）的健康报告；没有 / 读不了返回 None。"""
    d = health_dir(state_dir)
    try:
        cands = [d / f"{run_id}.json"] if run_id else list(reversed(_reports(d)))
    except OSError:
        return None
    for p in cands:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _gb(b) -> str:
    return "?" if b is None else f"{b / 1e9:.1f} GB"


def render(rep: dict, path=None) -> list[str]:
    """报告的紧凑文字版：`run` 末尾与 `media-agent health` 共用。"""
    status = rep.get("status") or "?"
    reasons = rep.get("reasons") or []
    dur = rep.get("duration_s")
    lines = [f"\n═══ 健康：{ICON.get(status, '·')} {status}"
             + (f"（{len(reasons)} 条）" if reasons else "")
             + f"  批次 {rep.get('run_id')}"
             + (f"  用时 {dur:.0f}s" if isinstance(dur, (int, float)) else "")
             + f"  退出码 {rep.get('exit_code')} ═══"]
    mark = {"ok": "✓"}
    cl = rep.get("clients") or {}
    if cl:
        lines.append("  客户端  " + "  ".join(
            f"{name} {mark.get(str(cl.get(k)), '–' if str(cl.get(k)).startswith('off') else '✗')}"
            for k, name in (("qbit", "qBit"), ("ab", "AB"), ("tmdb", "TMDB"), ("llm", "LLM"))))
    t = rep.get("torrents") or {}
    if t:
        lines.append(f"  种子    {t.get('current') if t.get('current') is not None else '读不到'}"
                     + (f"（上一轮 {t['previous']}）" if t.get("previous") is not None else ""))
    a = rep.get("actions")
    if a:
        lines.append(f"  执行    执行 {a['applied']} · 跳过 {a['skipped']} · 失败 {a['failed']} · "
                     f"未确认 {a['unknown']} · 审计转写 {a['audit_problems']}")
    g = rep.get("grab")
    if g and (g["proposed"] or g["applied"] or g["failed"] or g["unknown"]):
        lines.append(f"  抓取    提议 {g['proposed']} · 加上 {g['applied']} · 已存在(409) "
                     f"{g['already_present']} · 元数据超时 {g['metadata_timeouts']} · 失败 {g['failed']}"
                     + (f" · 未确认 {g['unknown']}" if g["unknown"] else ""))
    det = rep.get("detectors")
    if det:
        lines.append(f"  规则    {det['count']} 条 · 崩溃 {len(det['errors'])}"
                     + "".join(f"\n            {e['rule']}: {e['error'][:80]}（{e['where']}）"
                               for e in det["errors"][:5]))
    tr = rep.get("trash") or {}
    if tr:
        disp = tr.get("disposal") or {}
        lines.append(f"  隔离区  {_gb(tr.get('bytes'))} / {tr.get('files', '?')} 个文件 · 剩余 "
                     f"{_gb(tr.get('free_bytes'))}（阈值 {_gb(tr.get('min_free_bytes'))}）"
                     + (f" · 本轮删 {disp.get('deleted', 0)}（{_gb(disp.get('freed_bytes'))}）"
                        if disp else ""))
    st = rep.get("stuck")
    if st and (st["open"] or st["acked"]):
        lines.append(f"  卡住    {len(st['open'])}" + (f"（另 {st['acked']} 个已确认）" if st["acked"] else ""))
    un = rep.get("unrenamed")
    if un and un.get("count"):
        lines.append(f"  未改名  {un['count']} 个发布名文件超过 {un['threshold_hours']:g} 小时")
    le = rep.get("logged_errors") or {}
    if le.get("count"):
        top = "、".join(f"[{k}] {v}" for k, v in sorted(le["by_tag"].items(), key=lambda kv: -kv[1])[:4])
        lines.append(f"  日志    {le['count']} 行报错（{top}）")
    for r in reasons:
        lines.append(f"  {ICON.get(r['level'], '·')} {r['text']}")
    nt = rep.get("notify") or {}
    if nt.get("sent"):
        lines.append(f"  ✉️  已发通知：{nt.get('subject', '')}")
    elif nt.get("error"):
        lines.append(f"  ⚠️  通知邮件没发出去：{nt['error']}（下一轮重试）")
    if path:
        lines.append(f"  报告 {path}（media-agent health [--run ID] [--json]）")
    return lines
