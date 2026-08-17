"""把每部番的采集档案（sidecar）与实际状态同步。

它不产出"问题"，而是**维护 media-agent 自己的记忆**：规范名、见过的别名、
当前用的字幕组、各季进度。这份记忆跟着目录走，AutoBangumi 的库漂移或炸掉都不受影响。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .. import sidecar as sc_mod
from ..cache import Cache
from ..kernel import Action, Context, Finding, LibraryState
from ..naming import VIDEO_EXTS
from .subscription import SEASONAL_WINDOW_DAYS, _fetch_rss_titles, _patterns_of


class SidecarSyncDetector:
    """维护 `.media-agent.json`。

    产出的 finding 只有一种：档案内容与实际状态不一致，需要更新。
    动作是纯写文件、完全可逆，且不碰任何媒体内容。
    """
    id = "sidecar-sync"
    kind = "sidecar_stale"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        cache = Cache(ctx.config.cache_db)
        today = date.today()
        cutoff = today - timedelta(days=SEASONAL_WINDOW_DAYS)

        # 订阅 → 目录：走 save_path，这是唯一被同步维护的关联
        b_by_dir: dict[str, dict] = {}
        root = ctx.config.media_root.name
        for b in state.bangumi_rows:
            sp = b.get("save_path") or ""
            parts = Path(sp).parts
            if root in parts:
                i = parts.index(root)
                if i + 1 < len(parts):
                    b_by_dir.setdefault(parts[i + 1], b)

        for show in state.shows:
            sc = sc_mod.load(show.dir_path)
            changed = []

            if sc.canonical_title != show.dir_name:
                sc.canonical_title = show.dir_name
                changed.append("规范名")
            if show.tmdb_id and sc.tmdb_id != show.tmdb_id:
                sc.tmdb_id, sc.tmdb_title = show.tmdb_id, show.tmdb_title
                changed.append("TMDB")

            b = b_by_dir.get(show.dir_name)
            if b:
                if sc.bangumi_id != b["id"]:
                    sc.bangumi_id = b["id"]
                    changed.append("订阅id")
                # 记录当前源
                grp = b.get("group_name") or ""
                link = b.get("rss_link") or ""
                cur = sc.current_source
                if grp and (not cur or cur.get("group") != grp
                            or cur.get("rss_link") != link):
                    sc.adopt_source(grp, link, reason="扫描时同步")
                    changed.append(f"当前源→{grp}")
                # 把实际发布标题里见过的名字记下来，作为将来匹配失效时的备用别名
                if link:
                    ck = f"rss:{link}"
                    cached = cache.get_llm(ck)
                    titles = cached.get("titles", []) if cached else []
                    if titles:
                        eps = set()
                        for t in titles:
                            m = re.search(r"[-\[]\s*(\d{1,3})(?:v\d)?\s*[\]\[（(]", t)
                            if m:
                                eps.add(int(m.group(1)))
                        if eps and sc.current_source:
                            hi = max(eps)
                            if sc.current_source.get("last_episode") != hi:
                                sc.current_source["last_episode"] = hi
                                changed.append("源进度")
                        for p in _patterns_of(b):
                            if sc.add_alias(p):
                                changed.append("别名")

            # 各季进度
            have: dict[int, set[int]] = defaultdict(set)
            for f in show.files:
                if f.is_incomplete or f.path.suffix.lower() not in VIDEO_EXTS:
                    continue
                m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", f.filename)
                if m:
                    have[int(m.group(1))].add(int(m.group(2)))

            for sn, got in sorted(have.items()):
                key = str(sn)
                entry = {"have": sorted(got)}
                if show.tmdb_id:
                    ck = f"tmdbeps:{show.tmdb_id}:{sn}"
                    cached = cache.get_llm(ck)
                    eps = cached.get("eps", []) if cached else []
                    dated = []
                    for e in eps:
                        if e.get("air_date"):
                            try:
                                dated.append((date.fromisoformat(e["air_date"]),
                                              e["episode_number"]))
                            except ValueError:
                                pass
                    if dated:
                        entry["total"] = len(eps)
                        entry["aired"] = len([n for d, n in dated if d <= today])
                        nxt = sorted(d for d, _ in dated if d > today)
                        entry["next_air"] = nxt[0].isoformat() if nxt else "已完结"
                        entry["seasonal"] = min(d for d, _ in dated) >= cutoff
                if sc.seasons.get(key) != entry:
                    sc.seasons[key] = entry
                    changed.append(f"S{sn}进度")

            if not changed:
                continue
            yield Finding(
                rule=self.id, kind=self.kind, severity="minor",
                summary=f"采集档案需更新：{'、'.join(dict.fromkeys(changed))}",
                show=show.dir_name, path=str(sc_mod.path_for(show.dir_path)),
                evidence={"changed": list(dict.fromkeys(changed))},
                action=Action(op="write_sidecar",
                              args={"show_dir": str(show.dir_path),
                                    "payload": sc_mod.asdict_of(sc)},
                              note="只写 .media-agent.json，不碰媒体内容"),
            )


SIDECAR_DETECTORS = [SidecarSyncDetector]
