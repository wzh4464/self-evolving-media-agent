"""通知邮件：健康报告**有变化**时发一封（SMTP over SSL），没配置就什么都不做。

**为什么只发变化**：健康报告写在生产机的 `state/health/` 里，没人去看就等于没写；可每轮都发一封，第三天就没人
看邮件了。所以只在这些时候发：

- 状态变坏：ok → warn、ok / warn → critical（第一轮就不是 ok 也算）；
- 从 critical 出来（critical → ok / warn）。warn → ok 不发——没什么要人做的；
- 新出现的卡住问题（同一个指纹只发一次；它消失之后再出现，再发）——状态一直是 warn 也照发；
- 新进入整批拒绝（连着几轮拒绝只发第一轮）；审计开始转写到备用文件（同上）。

**一轮最多一封**：这一轮的事件合在一封里。去重状态在 `state/notify.json`：`last_status`（人最后一次被告知的状态）、
`active`（正在持续的事件 → 第一次发出的时间：`stuck:<指纹>` / `degraded` / `audit_fallback`）、`failures`。
"消失"只认**评估过**的轮次：被锁挡住、维护暂停、整批拒绝、半路崩溃的一轮没走到卡住检测（或执行器），它不知道的
事件原样带到下一轮，不算消失。

**发不出去**（SMTP 连不上、认证失败……）：在 stderr 说一句、`failures` 加一、写进这一轮的健康报告，这一轮的事件
存进 `undelivered`（带批次与时间，最多 50 条，更早的只计数），下一封信开头补上。以前只是不推进 `last_status` 与
`active`、指望下一轮重算——可下一轮若已回到 ok，重算什么都没有，一次短暂的整批拒绝就谁也不知道了（2026-09-26 复审）。
去重状态照常推进：补发的与这一轮的不会重复。永远不拦这一轮、不改退出码。

**永不带密钥**：主题与正文都过 `redact`——配置里的密码 / key 原样替换成 `***`，URL 里 `api_key=`、`token=`、
`password=`，`Bearer …`、`user:pass@host` 也遮掉（httpx 的报错会把请求 URL 连同 TMDB 的 `api_key` 一起带出来）。
健康报告落盘前同样过一遍。

配置（`.env`）：`NOTIFY_EMAIL_TO`、`NOTIFY_SMTP_HOST`、`NOTIFY_SMTP_PORT`（默认 465）、`NOTIFY_SMTP_USER`、
`NOTIFY_SMTP_PASS`。收件人与主机都没配 = 关闭；只配了一半 = 关闭并在 stderr 提醒。发件人是 `NOTIFY_SMTP_USER`
（没配就用收件人）。
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

STATE_NAME = "notify.json"
SUBJECT_PREFIX = "[media-agent]"

# 从 critical 出来都算"恢复"；变坏的方向：ok → warn / critical，warn → critical
_RANK = {"ok": 0, "warn": 1, "critical": 2}

_URL_SECRET = re.compile(r"(?i)\b(api_key|apikey|access_token|token|password|passwd|secret|key)=([^&\s\"']+)")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def enabled(cfg) -> tuple[bool, str]:
    """(是否发通知, 没开的原因)。都没配 = 关闭、不是错误；只配了一半要提醒。"""
    to, host = (cfg.notify_email_to or "").strip(), (cfg.notify_smtp_host or "").strip()
    if to and host:
        return True, ""
    if not to and not host:
        return False, ""
    missing = "NOTIFY_SMTP_HOST" if to else "NOTIFY_EMAIL_TO"
    return False, f"通知邮件只配了一半（缺 {missing}），这一轮不发"


# 配置里的密钥按原文替换的最短长度。更短的（`test` 这种）原文替换会把路径、番名里碰巧相同的字也换掉——
# 健康报告里的路径就此对不上（测试里实际发生过）；它们只会随 URL / 请求头出现，由下面的模式遮掉。
_LITERAL_MIN = 8


def redact(cfg, text: str) -> str:
    """遮掉配置里的密钥与常见的 URL / 请求头里的凭据。"""
    out = str(text)
    for name in ("qbit_pass", "ab_pass", "tmdb_api_key", "llm_key", "notify_smtp_pass"):
        secret = str(getattr(cfg, name, "") or "")
        if len(secret) >= _LITERAL_MIN:
            out = out.replace(secret, "***")
    out = _URL_SECRET.sub(lambda m: f"{m.group(1)}=***", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} ***", out)
    return _USERINFO.sub(lambda m: f"{m.group(1)}***@", out)


def redact_obj(cfg, obj):
    """对一份 JSON 能表示的数据整体遮一遍（健康报告落盘前用）。"""
    try:
        return json.loads(redact(cfg, json.dumps(obj, ensure_ascii=False, default=str)))
    except (TypeError, ValueError):
        return obj


def _state_path(cfg) -> Path:
    return Path(cfg.state_dir) / STATE_NAME


def load_state(cfg) -> dict:
    try:
        data = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("active", {})
            data.setdefault("failures", 0)
            return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass                                        # 没有 / 坏了：当作第一次（最多多发一封，不会少发）
    return {"last_status": None, "active": {}, "failures": 0}


def _save_state(cfg, st: dict) -> None:
    p = _state_path(cfg)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def events(report: dict, st: dict) -> tuple[list[dict], dict]:
    """这一轮值得告诉人的事，以及"告诉了之后"的去重状态。"""
    now = report.get("finished") or datetime.now().isoformat(timespec="seconds")
    cur, prev = report.get("status") or "ok", st.get("last_status")
    out: list[dict] = []
    if prev != cur:
        worse = _RANK.get(cur, 0) > _RANK.get(prev or "ok", 0)
        if worse or prev == "critical":
            out.append({"kind": "status", "text": f"状态 {prev or '（首次）'} → {cur}",
                        "recovery": prev == "critical" and not worse})
    active_before = dict(st.get("active") or {})
    active: dict = {}
    # 这一轮**没评估**的事件原样带着，只有评估了、而且不在了才算"消失"。被锁挡住 / 维护暂停 / 整批拒绝 / 半路
    # 崩溃的一轮走不到卡住检测（报告里 stuck 是 None）、暂停与被锁挡住的也走不到执行器（actions 是 None）。以前
    # `active` 每轮从零重建，这样的一轮把 `stuck:<指纹>` 全清了，下一轮正常的 run 把每个一直卡着的指纹当「新卡住」
    # 再发一遍——每次 qBit 超时、部署撞上一轮、看门狗重建容器、救援暂停都会（2026-09-26 复审，生产 8 月底同时
    # 卡着 58–66 个）。
    stuck = report.get("stuck")
    if stuck is None:
        active.update({k: v for k, v in active_before.items() if k.startswith("stuck:")})
    for s in (stuck or {}).get("open") or []:
        key = f"stuck:{s['fp']}"
        active[key] = active_before.get(key, now)
        if key not in active_before:
            out.append({"kind": "stuck", "fp": s["fp"],
                        "text": (f"新卡住：[{s['rule']}] {s['kind']}【{s['show'] or '-'}】连续 {s['runs']} 轮"
                                 f"（自 {s['first_seen']}），指纹 {s['fp']}——{s['summary'][:120]}"
                                 f"（要人处理、先不提醒：media-agent ack {s['fp']} --reason …）")})
    acted = report.get("actions") is not None          # 执行器跑完了：拒不拒绝、审计写没写进去都有了定论
    refused = (report.get("degraded") or {}).get("refused")
    if refused:
        active["degraded"] = active_before.get("degraded", now)
        if "degraded" not in active_before:
            out.append({"kind": "degraded", "text": f"新进入整批拒绝：{refused[:300]}"})
    elif not acted and "degraded" in active_before:
        active["degraded"] = active_before["degraded"]
    if not acted:
        if "audit_fallback" in active_before:
            active["audit_fallback"] = active_before["audit_fallback"]
    elif report["actions"].get("audit_problems"):
        active["audit_fallback"] = active_before.get("audit_fallback", now)
        if "audit_fallback" not in active_before:
            from .health import audit_where
            out.append({"kind": "audit_fallback",
                        "text": (f"{report['actions']['audit_problems']} 条审计没能原样写进 audit.jsonl："
                                 f"{audit_where(report['actions'])}")})
    return out, {"last_status": cur, "active": active}


# 发不出去时留着补发的事件最多这么多条（SMTP 一直配错时 notify.json 不能无限长）；更早的只计数
_UNDELIVERED_MAX = 50


def _late(e: dict) -> dict:
    """上一次没发出去、这一封补上的事件：写明是哪一批、什么时候的。"""
    return {**e, "late": True, "text": f"（补发：批次 {e.get('run_id')}，{e.get('at')}）{e['text']}"}


def _subject(report: dict, evs: list[dict]) -> str:
    from .health import ICON
    status = report.get("status") or "?"
    st = next((e for e in evs if e["kind"] == "status" and not e.get("late")), None)
    if st and st.get("recovery"):
        head = f"{ICON.get(status, '')} 恢复 {status}"
    else:
        head = f"{ICON.get(status, '')} {status}"
    first = next((r["text"] for r in report.get("reasons") or []), "")
    tail = first or next((e["text"] for e in evs), "")
    return f"{SUBJECT_PREFIX} {head}：{tail}"[:180]


def compose(cfg, report: dict, evs: list[dict]) -> EmailMessage:
    """一封信：纯文本 + 一小段 HTML。全文过 `redact`。"""
    from .health import render
    lines = [f"media-agent 批次 {report.get('run_id')}（{report.get('finished')}）", ""]
    lines += [f"• {e['text']}" for e in evs]
    lines += [""] + [ln.lstrip("\n") for ln in render(report)]
    lines += ["", "详情：media-agent health --run " + str(report.get("run_id"))]
    text = redact(cfg, "\n".join(lines))
    msg = EmailMessage()
    msg["Subject"] = redact(cfg, _subject(report, evs))
    sender = (cfg.notify_smtp_user or cfg.notify_email_to).strip()
    msg["From"] = formataddr(("media-agent", sender))
    msg["To"] = cfg.notify_email_to.strip()
    msg.set_content(text)
    items = "".join(f"<li>{html.escape(redact(cfg, e['text']))}</li>" for e in evs)
    msg.add_alternative(
        f"<p><b>media-agent</b> 批次 <code>{html.escape(str(report.get('run_id')))}</code></p>"
        f"<ul>{items}</ul><pre>{html.escape(text)}</pre>", subtype="html")
    return msg


def send(cfg, msg: EmailMessage) -> None:
    """SMTP over SSL（默认 465）。会抛异常——调用方（`maybe_send`）接住。"""
    with smtplib.SMTP_SSL(cfg.notify_smtp_host.strip(), int(cfg.notify_smtp_port), timeout=30,
                          context=ssl.create_default_context()) as s:
        if cfg.notify_smtp_user:
            s.login(cfg.notify_smtp_user, cfg.notify_smtp_pass)
        s.send_message(msg)


def maybe_send(cfg, report: dict) -> dict:
    """按这一轮的健康报告决定发不发、发一封。返回写进健康报告的结果。**永不抛异常。**"""
    on, why = enabled(cfg)
    if not on:
        if why:
            _stderr(f"⚠️  {why}")
        return {"enabled": False, **({"note": why} if why else {})}
    try:
        st = load_state(cfg)
        evs, after = events(report, st)
        pending = [e for e in st.get("undelivered") or [] if isinstance(e, dict) and e.get("text")]
        out = [_late(e) for e in pending] + evs
        if st.get("undelivered_dropped"):
            out.insert(0, {"kind": "dropped", "late": True,
                           "text": (f"另有 {st['undelivered_dropped']} 条更早的事件一直没发出去、已不再补发"
                                    f"（media-agent health --run <批次> 看当时的报告）")})
        keep = {"last_sent": st["last_sent"]} if st.get("last_sent") else {}
        if not out:
            # 没有要说的：静静跟上此刻的状态（warn → ok 这种不发信的变化也记下来）
            _save_state(cfg, {**after, "failures": 0, **keep})
            return {"enabled": True, "events": [], "sent": False}
        msg = compose(cfg, report, out)
        try:
            send(cfg, msg)
        except Exception as e:                      # noqa: BLE001 —— 发不出去不拦这一轮；事件留着，下一封补上
            err = redact(cfg, f"{type(e).__name__}: {e}")
            failures = int(st.get("failures") or 0) + 1
            mine = [{**e, "run_id": report.get("run_id"), "at": report.get("finished")} for e in evs]
            queued = (pending + mine)[-_UNDELIVERED_MAX:]
            _save_state(cfg, {**after, **keep, "failures": failures, "last_error": err,
                              "undelivered": queued,
                              "undelivered_dropped": int(st.get("undelivered_dropped") or 0)
                              + len(pending) + len(mine) - len(queued)})
            _stderr(f"⚠️  通知邮件没发出去（第 {failures} 次）：{err}；{len(queued)} 条事件留到下一封补发")
            return {"enabled": True, "events": [e["text"] for e in out], "sent": False, "error": err}
        _save_state(cfg, {**after, "failures": 0,
                          "last_sent": datetime.now().isoformat(timespec="seconds")})
        return {"enabled": True, "events": [e["text"] for e in out], "sent": True,
                "subject": msg["Subject"]}
    except Exception as e:                          # noqa: BLE001 —— 通知是观测，任何意外都不拦这一轮
        err = redact(cfg, f"{type(e).__name__}: {e}")
        _stderr(f"⚠️  通知邮件没发出去：{err}")
        return {"enabled": True, "sent": False, "error": err}


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:                               # noqa: BLE001, S110 —— stderr 也写不进就没办法了
        pass
