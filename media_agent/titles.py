"""TMDB 标题的稳定闸：改名目标不跟着 TMDB 的一次抖动走。

**现场**（LAT-04，《鬼物语》）：`鬼物语/Season 1/` 的文件 2026-08-20 按 TMDB 46195 改成了 `物语系列 S01E0x`；
09-19 10:49 那一轮 30 天的缓存过期、重新搜索没有命中，标题退回目录名，文件被改回 `鬼物语 …`；16:51 又搜到了，
再改成 `物语系列 …`。每一次单独看都"合规"，放在一起就是在两个名字之间来回改，Jellyfin 的刮削记录跟着乱。

改名 / 目录名 / 分类用的标题（`Show.tmdb_title`，经 `official_title`）从这里取，规则是：

- **取不到标题**（TMDB 这一轮失败、缓存也没有）→ 用上次采用的标题，绝不退回目录名 / AB 标题；
- **标题变了** → 同一个 tmdb_id 的新标题要**连续两轮 `run`** 都看到才采用（`CONFIRM_RUNS`）；
  中间有一轮没看到（取不到、或看到的又是别的）就重新数；
- **改回去** → 一个标题被换掉之后 30 天内（`FLIP_WINDOW_DAYS`）TMDB 又给回它，不采用、报出来：
  那多半是 TMDB 条目在抖，或者缓存 / 搜索出了岔子，要人看；
- **人钉住的标题**（sidecar `pinned` 里有 `tmdb_title`）→ 扫描直接用它，这里不管、不记。

记录在 `state/titles.json`（不在仓库里、也不在媒体根下）：每个 tmdb_id 一条——采用的标题与从哪天起、正在确认的
新标题（看到几轮、哪一轮最后看到）、30 天内被换掉的标题。只有 `run` 记（`record`，扫描之后）；`diagnose` 只读。
第一次见到的 tmdb_id 没有记录时，以 sidecar 里的 `tmdb_title`（或旧缓存里的标题）为"已采用"——那是库里此刻
正在用的名字；TMDB 给的若与它不同，同样要连看两轮。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

NAME = "titles.json"
CONFIRM_RUNS = 2
FLIP_WINDOW_DAYS = 30


@dataclass
class Decision:
    """一个 tmdb_id 这一轮用哪个标题、为什么。

    status：
      stable        TMDB 给的就是已采用的
      first         第一次见、没有任何已知标题：直接采用
      adopted       新标题连续第 `CONFIRM_RUNS` 轮看到：这一轮起采用
      pending       新标题还没看够轮数：这一轮仍用旧的
      flip_blocked  TMDB 给回了 30 天内刚换掉的标题：不采用
      unresolved    这一轮取不到 TMDB 的标题：用已采用的
    人在 sidecar 里钉住的标题（`pinned` 里有 `tmdb_title`）不经过这里，扫描直接用它。
    """
    tmdb_id: int
    title: str                     # 这一轮改名 / 目录名 / 分类用的标题（空 = 什么都不知道）
    status: str
    observed: str = ""             # TMDB 这一轮给的（取不到为空）
    previous: str = ""             # 已采用的（pending / adopted / flip_blocked 时有意义）
    runs: int = 0                  # pending / adopted：连续看到几轮（含这一轮）
    left_at: str = ""              # flip_blocked：previous 之前的那个标题是哪天被换掉的


@dataclass
class Book:
    seq: int = 0                                    # 记过几轮（`record` 每次 +1），判"连续"用
    shows: dict[str, dict] = field(default_factory=dict)

    def entry(self, tmdb_id: int) -> dict:
        return self.shows.get(str(tmdb_id)) or {}

    def decide(self, tmdb_id: int, observed: str | None, *, fallback: str = "",
               now: datetime | None = None) -> Decision:
        """这一轮用哪个标题。`observed` = TMDB 这一轮给的（取不到为 None）；`fallback` = 没有记录时当作
        已采用的（sidecar 里的 `tmdb_title`、或旧缓存里的标题——库里此刻正在用的名字）。"""
        now = now or datetime.now()
        e = self.entry(tmdb_id)
        adopted = e.get("adopted") or fallback or ""
        observed = (observed or "").strip()
        if not observed:
            return Decision(tmdb_id, adopted, "unresolved", previous=adopted)
        if not adopted:
            return Decision(tmdb_id, observed, "first", observed=observed)
        if observed == adopted:
            return Decision(tmdb_id, adopted, "stable", observed=observed, previous=adopted)
        cutoff = now - timedelta(days=FLIP_WINDOW_DAYS)
        for old in e.get("left") or []:
            if old.get("title") == observed and _ts(old.get("at")) >= cutoff:
                return Decision(tmdb_id, adopted, "flip_blocked", observed=observed,
                                previous=adopted, left_at=old.get("at", ""))
        p = e.get("pending") or {}
        runs = (int(p.get("runs") or 0) + 1
                if p.get("title") == observed and p.get("seq") == self.seq else 1)
        if runs >= CONFIRM_RUNS:
            return Decision(tmdb_id, observed, "adopted", observed=observed, previous=adopted,
                            runs=runs)
        return Decision(tmdb_id, adopted, "pending", observed=observed, previous=adopted, runs=runs)


def _ts(s) -> datetime:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return datetime.min


def path_of(state_dir) -> Path:
    return Path(state_dir) / NAME


def load(state_dir) -> tuple[Book, str]:
    """读记录；没有就是空的。读不了 / 格式不对返回空记录 + 问题（调用方说出来）：这一轮按"没有记录"处理——
    以 sidecar 里的标题为已采用，最坏是一个新标题多等一轮，不会退回目录名。"""
    p = path_of(state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Book(), ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return Book(), f"{p.name} 读不了（{type(e).__name__}: {e}）"
    if not isinstance(data, dict) or not isinstance(data.get("shows", {}), dict):
        return Book(), f"{p.name} 格式不对"
    return Book(seq=int(data.get("seq") or 0), shows=dict(data.get("shows") or {})), ""


def apply(book: Book, decisions: dict[int, Decision], *, run_id: str = "",
          now: datetime | None = None) -> Book:
    """把这一轮的决定记进 `book`（原地改并返回）。`seq` 先 +1：这一轮就是第 `seq` 轮。"""
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    book.seq += 1
    cutoff = now - timedelta(days=FLIP_WINDOW_DAYS)
    for tid, d in decisions.items():
        e = dict(book.shows.get(str(tid)) or {})
        if d.title and not e.get("adopted"):
            e["adopted"], e["since"] = d.title, stamp  # 头一次记：把此刻在用的名字记下来
        if d.status in ("stable", "first"):
            e.pop("pending", None)
        elif d.status == "adopted":
            e["left"] = [*(e.get("left") or []), {"title": d.previous, "at": stamp}]
            e["adopted"], e["since"] = d.observed, stamp
            e.pop("pending", None)
        elif d.status == "pending":
            p = e.get("pending") or {}
            first = p.get("first_seen") if p.get("title") == d.observed else stamp
            e["pending"] = {"title": d.observed, "runs": d.runs, "seq": book.seq,
                            "first_seen": first or stamp, "run_id": run_id}
        elif d.status == "flip_blocked":
            e.pop("pending", None)
        e["left"] = [x for x in (e.get("left") or []) if _ts(x.get("at")) >= cutoff]
        if not e["left"]:
            e.pop("left")
        book.shows[str(tid)] = e
    return book


def record(state_dir, decisions: dict[int, Decision], *, run_id: str = "",
           now: datetime | None = None) -> str:
    """`run` 扫描之后调用：把这一轮的决定落盘。永不抛异常，写不进去返回问题（调用方说出来）——
    代价是下一轮少数一轮，不会让标题乱跳。没有任何决定（这一轮没问 TMDB）就不记、不算一轮。"""
    if not decisions:
        return ""
    book, problem = load(state_dir)
    if problem:
        return f"{problem}，这一轮没有记标题"
    apply(book, decisions, run_id=run_id, now=now)
    p = path_of(state_dir)
    tmp = ""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="." + p.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "seq": book.seq, "shows": book.shows}, fp,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:                          # noqa: BLE001 —— 记录写不进去不拦这一轮，把原因交给调用方说
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:                         # 已经换上去了 / 没建成：没有要清的
                pass
        return f"{p.name} 写不进去（{type(e).__name__}: {e}）"
    return ""
