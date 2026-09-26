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
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

FALLBACK_NAME = "audit.fallback.jsonl"


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
