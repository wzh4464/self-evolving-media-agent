"""TMDB 身份与标题的发现：模型选的条目要钉进 sidecar、标题在等确认 / 被拦下、这部番这一轮不改名。

扫描（`scan._resolve_tmdb`）只做决定、不写任何东西（AGENTS.md 第 13 条）；要落进 sidecar 的
（模型选的 tmdb_id）在这里变成动作，经执行器写、有审计、能回退。
"""
from __future__ import annotations

from typing import Iterable

from ..kernel import Action, Context, Finding, LibraryState
from ..titles import CONFIRM_RUNS, FLIP_WINDOW_DAYS


class TmdbIdentityDetector:
    id = "tmdb-identity"
    kind = "tmdb_pick"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for p in state.tmdb_proposals:
            # critic N4：以前模型每轮在多个候选里重新选一次，直接决定改名目标 / 目录名 / 分类，谁也看不见。
            # 现在它只选一次（缓存），这一轮不用；钉进 sidecar 之后下一轮起照钉住的认，不对就改 sidecar。
            conf = p.get("confidence")
            yield Finding(
                rule=self.id, kind="tmdb_pick", severity="important",
                summary=(f"TMDB 按「{p.get('query')}」搜到 {len(p.get('candidates') or [])} 个候选，模型选了 "
                         f"{p['id']}「{p.get('title')}」（置信 {conf}：{p.get('reason') or '无理由'}）——"
                         f"钉进 sidecar，下一轮起按它认；不对就改 sidecar 的 tmdb_id"),
                show=p["show"],
                evidence={k: p.get(k) for k in ("id", "title", "query", "confidence", "reason",
                                                "candidates")},
                action=Action(op="pin_tmdb",
                              args={"show_dir": p["show_dir"], "tmdb_id": int(p["id"]),
                                    "title": p.get("title") or "", "source": "llm"},
                              note="只在 sidecar 还没有 tmdb_id 时写；已有的一律不改"),
            )

        reported: set[int] = set()
        for show in state.shows:
            if show.naming_hold:
                yield Finding(
                    rule=self.id, kind="naming_held", severity="important", classified=True,
                    summary=f"这一轮不按标题改名（改名、目录名、分类、NFO、抓取）：{show.naming_hold}",
                    show=show.dir_name, evidence={"reason": show.naming_hold,
                                                  "tmdb_id": show.tmdb_id})
            d = state.title_decisions.get(show.tmdb_id) if show.tmdb_id else None
            if d is None or show.tmdb_id in reported:
                continue
            if d.status == "pending":
                reported.add(show.tmdb_id)
                yield Finding(
                    rule=self.id, kind="tmdb_title_pending", severity="minor", classified=True,
                    summary=(f"TMDB {d.tmdb_id} 的标题变了：「{d.previous}」→「{d.observed}」，第 {d.runs} 轮看到；"
                             f"连续 {CONFIRM_RUNS} 轮才采用，这之前改名 / 目录名 / 分类仍用旧的"),
                    show=show.dir_name,
                    evidence={"tmdb_id": d.tmdb_id, "adopted": d.previous, "observed": d.observed,
                              "runs": d.runs})
            elif d.status == "flip_blocked":
                reported.add(show.tmdb_id)
                yield Finding(
                    rule=self.id, kind="tmdb_title_flip_blocked", severity="important", classified=True,
                    summary=(f"TMDB {d.tmdb_id} 的标题要改回「{d.observed}」——{d.left_at[:10]} 才从它换成"
                             f"「{d.previous}」，{FLIP_WINDOW_DAYS} 天内不来回改名（LAT-04）。确实该用它：在 sidecar 里"
                             f"写 tmdb_title 并把 \"tmdb_title\" 加进 pinned"),
                    show=show.dir_name,
                    evidence={"tmdb_id": d.tmdb_id, "adopted": d.previous, "observed": d.observed,
                              "left_at": d.left_at})


IDENTITY_DETECTORS = [TmdbIdentityDetector]
