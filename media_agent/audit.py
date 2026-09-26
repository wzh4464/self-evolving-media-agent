"""审计日志 `state/audit.jsonl` 的读写。

`audit.jsonl` 是回退（`rollback` / `runs` / `repair`）、隔离区处置（`purge`）、失败模式统计
（`evolution.find_failure_patterns`）的唯一依据。它只增不删，生产上已有约 9.6k 行、好几代格式
（最早的没有 `run_id` / `undo`，回退汇总行只有 `ts` / `run_id` / `status` 与计数）。读的一方一律
容忍：坏行、缺字段、不认识的状态都跳过或原样带过，不抛异常。

**写永不抛异常**（B1，critic N8 的余项）。以前写失败（磁盘满、权限、`json` 序列化不了）的异常
冲出执行器：改动做了、记录没有，整轮连隔离区处置一起中止——而磁盘满正是最需要处置腾空间的时候。
现在：

1. 序列化不了的值（`Path`、`set`……）按 `str` 降级写进主审计，记录里标 `audit_degraded`；
2. 主审计写不进去，就把**同一行**原样写到 stderr，并尽力追加到同目录的 `audit.fallback.jsonl`；
3. 读的一方（`iter_records`）两个文件一起读——转写的记录照样能回退、照样出现在 `runs` 里。

每一处降级 / 转写都由调用方计数，在本轮输出与退出码里大声说（`ExecReport.audit_problems`）。

**状态**（`status`）——执行器每条动作记录恰好一个：

- `applied`：改动生效了，**已确认**（正常返回，或调用抛了异常、但按此刻状态核实确实生效）。
  可逆的带 `undo`。
- `skipped`：没动手（dry-run、闸门拒绝、状态已变……），`reason` 说明。什么都没改。
- `failed`：没生效——要么异常发生在发出任何改动之前，要么改动调用出错后按此刻状态核实**没有**生效。
  `error` 说明；若有已发生的附带改动（如隔离时种子已摘、文件没搬走）以字段写明。
- `unknown`：改动**也许**生效了、执行器确认不了（改动调用出错后读不到此刻状态，或异常发生在已经发出
  改动之后）。`error` 是异常，`reason` 说明为什么确认不了；`effects_attempted` 列出已发出的改动；
  有"如果生效了该怎么撤"时照样带 `undo`——回退会按此刻状态核对后尝试它（每个逆操作动手前都核对）。

回退另写两种记录：

- **逐步**（每一步逆操作一条）：`run_id` = `rollback-of-<被回退的批次>`，`rollback_of` = 被回退的批次，
  `rollback_id` = 这一次回退自己的 ID，`op` = `undo:<逆操作>`，`args` = 逆操作，`undoes` 指回原记录
  （`seq` / `ts` / `op` / `status`）；`status` 同上四种（还原了 / 核对后跳过 / 没动就出错 / 动了之后出错）。
  不带 `undo`：回退不能再回退。
- **汇总**（一次回退一条）：`status` = `rollback`，`run_id` 是被回退的批次（生产上 4 条历史汇总也是这个形状，
  `list_runs` 靠它标"已回退"），`rollback_run_id` 指向逐步记录的批次号，另有计数。

不认识的状态一律当作"不是已生效"读：不计入回退、不计入已隔离。
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

FALLBACK_NAME = "audit.fallback.jsonl"

APPLIED, SKIPPED, FAILED, UNKNOWN = "applied", "skipped", "failed", "unknown"
# 执行器动作记录的全部状态（见模块文档）
STATUSES = (APPLIED, SKIPPED, FAILED, UNKNOWN)
# 回退会尝试其 `undo` 的状态：已生效的，与也许生效了的（逆操作动手前各自核对此刻状态）
UNDOABLE = (APPLIED, UNKNOWN)
# 回退汇总记录的状态
ROLLBACK = "rollback"
# 回退逐步记录的批次号前缀：`rollback-of-<被回退的批次>`（记录里另有 `rollback_of` 字段）
ROLLBACK_PREFIX = "rollback-of-"


def fallback_path(audit_log: Path) -> Path:
    """主审计写不进去时的备用文件：与 audit.jsonl 同目录。"""
    return Path(audit_log).with_name(FALLBACK_NAME)


def dumps(rec: dict) -> tuple[str, str]:
    """一条记录的 JSON 行，以及降级原因（没有降级为空串）。**永不抛异常。**

    先按原样序列化；不行就把序列化不了的值换成 `str`（`Path` 就是它的路径——回退照样能用），
    并在记录里标 `audit_degraded`；再不行（循环引用之类）只留识别这一条所需的几个字段。
    """
    try:
        return json.dumps(rec, ensure_ascii=False), ""
    except Exception as e:                          # noqa: BLE001 —— 写审计永不抛
        why = f"{type(e).__name__}: {e}"
    try:
        return json.dumps({**rec, "audit_degraded": why}, ensure_ascii=False,
                          default=str, skipkeys=True), why
    except Exception:                               # noqa: BLE001, S110 —— 再退一步，只留识别字段
        pass
    keep = ("ts", "run_id", "seq", "status", "dry_run", "rule", "kind", "op", "summary")
    minimal = {k: rec.get(k) if isinstance(rec.get(k), (str, int, float, bool, type(None)))
               else repr(rec.get(k)) for k in keep}
    minimal.update(args=repr(rec.get("args")), undo=repr(rec.get("undo")), audit_degraded=why)
    return json.dumps(minimal, ensure_ascii=False, default=repr), why


def append_line(path: Path, line: str) -> None:
    """追加一行。**会抛异常**——调用方（`write`）接住它。

    文件末尾不是换行（上一次写到一半：磁盘满、进程被杀）时先补一个换行。否则这一条会和那半行粘成
    一行坏 JSON，读的人把它们一起跳过——这一条的回退就此找不到。
    """
    data = (line + "\n").encode("utf-8")
    with open(path, "a+b") as fp:
        if fp.seek(0, os.SEEK_END) > 0:
            fp.seek(-1, os.SEEK_END)
            if fp.read(1) != b"\n":
                data = b"\n" + data
        fp.write(data)


def write(audit_log: Path, rec: dict) -> list[str]:
    """把一条记录写进审计。**永不抛异常**；返回这一条遇到的问题（空列表 = 原样写进了主审计）。"""
    line, degraded = dumps(rec)
    problems = [f"序列化降级、按字符串写入（{degraded}）"] if degraded else []
    try:
        append_line(Path(audit_log), line)
        return problems
    except Exception as e:                          # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    problems.append(f"写不进 audit.jsonl（{err}），已原样转写到 stderr 与 {FALLBACK_NAME}")
    _stderr(f"[audit-fallback] 审计写不进 {audit_log}（{err}），原样转写如下：\n{line}")
    fb = fallback_path(audit_log)
    try:
        append_line(fb, line)
    except Exception as e2:                         # noqa: BLE001
        why = f"{type(e2).__name__}: {e2}"
        problems[-1] = (f"写不进 audit.jsonl（{err}），{FALLBACK_NAME} 也写不进（{why}），"
                        f"只剩 stderr 里的一份")
        _stderr(f"[audit-fallback] {fb} 也写不进（{why}）：上面那一行只剩 stderr 里这一份")
    return problems


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:                               # noqa: BLE001, S110 —— stderr 也写不进就真的没办法了
        pass


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        _stderr(f"⚠️  读不了审计 {path}：{type(e).__name__}: {e}")
        return []


def _parse(path: Path) -> Iterator[dict]:
    for raw in _read_lines(path):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def _key(rec: dict) -> tuple | None:
    if rec.get("seq") is None:
        return None
    return (rec.get("run_id"), rec.get("seq"), rec.get("ts"), rec.get("op"), rec.get("status"))


def iter_records(audit_log: Path) -> Iterator[dict]:
    """主审计与 `audit.fallback.jsonl` 里的全部记录（先主后备）。坏行、不是对象的行跳过。

    备用文件里的一条若主审计里也有（主审计写到一半报了错、其实写进去了），只出主审计那一条：
    按 `(run_id, seq, ts, op, status)` 认，`seq` 是它在本批次里的序号（2026-09-26 第 3 阶段起才有；
    旧记录没有，也不会进备用文件）。同一个文件里的记录从不去重。
    """
    main = list(_parse(Path(audit_log)))
    yield from main
    have = {k for k in map(_key, main) if k is not None}
    for rec in _parse(fallback_path(audit_log)):
        k = _key(rec)
        if k is None or k not in have:
            yield rec
