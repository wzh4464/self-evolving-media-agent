"""先到先得的抓取器：不锁字幕组，按集号补齐。

与既有订阅模型的区别——

  旧：订阅锁定**一个字幕组**的 feed，靠 `filter` 黑名单挡掉其余。
      优点是不会重复，缺点是那个组慢/停更就整部番卡住，
      而且换源要人工介入（本 session 已经手工换过三次）。

  新：盯**整个番组页**（全字幕组），缺哪集就在当时可得的候选里挑最优的抓。
      判重不靠"锁定一个源"，靠 sidecar 里的 `seasons[N].have` 集号清单。

判重必须放在这里而不是 AutoBangumi：AB 的 `filter` 只是文件名黑名单，
表达不了"这集我已经有了"。

"最优"的定义见 `preferences.py`：硬门槛（必须有中文字幕）+ 偏好打分
（无删减 > 简中 > 内封）。**不为了快而收一个看不了的**——实测尼古喵喵 E08
当天只有 ABEMA 转载版，抓下来 711MB、零字幕轨。
"""
from __future__ import annotations

import html
import re
from datetime import date
from typing import Iterable

from .. import preferences
from ..cache import Cache
from ..kernel import Action, Context, Finding, LibraryState
from ..sidecar import load as load_sidecar
from .subscription import MIKAN, _http_get, _mikan_search_ids

# 单轮为一部番最多提议抓几集，防止新订阅时一次刷屏
MAX_PER_SHOW = 6


def _episode_of(title: str) -> int | None:
    """从发布标题里取集号。

    只认有明确分隔符的形态，避免把分辨率/年份当集号：
    `S01E08` / `- 08 ` / `[08]` / `第08集`。
    另外排除紧跟 p 的数字（1080p）与四位数（年份）。
    """
    for pat in (r"S\d{1,2}E(\d{1,3})",
                r"第\s*(\d{1,3})\s*[集话話]",
                r"[\[【]\s*(\d{1,3})(?:v\d)?\s*[\]】]",
                r"(?:\s|-)\s*(\d{1,3})(?:v\d)?\s*(?:\[|\(|$|\s)"):
        for m in re.finditer(pat, title):
            n = int(m.group(1))
            tail = title[m.end(1):m.end(1) + 1]
            if tail == "p":                 # 1080p / 720p
                continue
            if 1 <= n <= 200:
                return n
    return None


def _feed_items(bangumi_id: str) -> list[dict]:
    """整个番组页的 RSS（不带 subgroupid 即为全字幕组），按发布时间倒序。"""
    body = _http_get(f"{MIKAN}/RSS/Bangumi?bangumiId={bangumi_id}")
    out: list[dict] = []
    for it in re.findall(r"<item>(.*?)</item>", body, re.S):
        m = re.search(r"<title>(.*?)</title>", it, re.S)
        u = re.search(r'<enclosure[^>]*url="([^"]+)"', it)
        if not (m and u):
            continue
        out.append({"title": html.unescape(m.group(1)).strip(),
                    "url": html.unescape(u.group(1)).strip()})
    return out


def _resolve_mikan_id(sc, show, cache) -> str | None:
    """找这部番在 Mikan 上的番组 id，找到后写回 sidecar 省得每轮再搜。

    优先从已有的 rss_link 里抠——番组式链接里就带着 bangumiId。
    抠不到再按标题和别名搜，搜到的结果缓存。
    """
    if getattr(sc, "mikan_id", None):
        return str(sc.mikan_id)
    for s in sc.sources:
        m = re.search(r"bangumiId=(\d+)", s.get("rss_link") or "")
        if m:
            return m.group(1)
    for kw in [sc.canonical_title, show.official_title, *sc.aliases]:
        kw = (kw or "").strip()
        if not kw:
            continue
        ck = f"mikansearch:{kw}"
        hit = cache.get_llm(ck)
        if hit is None:
            try:
                hit = {"ids": _mikan_search_ids(kw, 3)}
            except Exception:
                continue
            cache.put_llm(ck, hit)
        ids = hit.get("ids") or []
        if ids:
            return ids[0]
    return None


class EpisodeAvailableDetector:
    """已播出、本地没有、而且此刻已经有可接受版本的集数。

    "可接受"由 preferences 判定；只在**当下**可得的候选里挑，不等更好的——
    这就是"谁先出要谁"。等不到合格版本的集数会一直报，直到有人做出来。
    """
    id = "episode-available"
    kind = "episode_grabbable"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not (ctx.tmdb and ctx.tmdb.enabled):
            return
        cache = Cache(ctx.config.cache_db)
        rules = preferences.load_rules()
        today = date.today().isoformat()

        # 多个目录解析到同一个 tmdb_id 时，只认"内容最多"的那个为宿主。
        # 不加这道闸的话，每个空壳目录都会以为自己缺整季、轮流去抓同一集：
        # 实测《入间同学入魔了！》的三个历史分季目录（！ / ！！ / ！！！）
        # 都写着 tmdb_id=91801 且都认领 season 4，于是连续三天、每 6 小时
        # 为其中一个抓一次 S04E19。qBittorrent 靠 infohash 挡住了重复下载，
        # 所以没造成实际损害——但也正因如此，空转了三天没人发现。
        owner: dict[int, object] = {}
        for s in state.shows:
            if not s.tmdb_id:
                continue
            cur = owner.get(s.tmdb_id)
            if cur is None or len(s.files) > len(cur.files):
                owner[s.tmdb_id] = s
        dup_reported: set[int] = set()

        for show in state.shows:
            if not show.tmdb_id:
                continue
            host = owner.get(show.tmdb_id)
            if host is not None and host is not show:
                if show.tmdb_id not in dup_reported:
                    dup_reported.add(show.tmdb_id)
                    sibs = [s.dir_name for s in state.shows
                            if s.tmdb_id == show.tmdb_id]
                    yield Finding(
                        rule=self.id, kind="duplicate_show_dir", severity="important",
                        summary=(f"{len(sibs)} 个目录指向同一个 TMDB 条目 "
                                 f"{show.tmdb_id}，抓取只认文件最多的"
                                 f"「{host.dir_name}」，其余会被跳过"),
                        show=host.dir_name,
                        evidence={"tmdb_id": show.tmdb_id, "dirs": sibs,
                                  "host": host.dir_name,
                                  "hint": "多半是历史遗留的分季目录，应合并到一个"},
                    )
                continue
            sc = load_sidecar(show.dir_path)
            if not sc.seasons:
                continue

            for season_key, info in sc.seasons.items():
                # 常年连载番（哆啦A梦之流）不参与——sidecar 扫描时已判好
                if not info.get("seasonal", False):
                    continue
                have = set(info.get("have") or [])
                try:
                    eps = ctx.tmdb.season_episodes(show.tmdb_id, int(season_key))
                except Exception:
                    continue
                aired = {e["episode_number"] for e in eps
                         if e.get("air_date") and e["air_date"] <= today}
                missing = sorted(aired - have)
                if not missing:
                    continue

                mid = _resolve_mikan_id(sc, show, cache)
                if not mid:
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="minor",
                        summary=(f"「{show.official_title}」缺 {len(missing)} 集"
                                 f"{missing[:8]}，但找不到 Mikan 番组页，无从抓取"),
                        show=show.dir_name,
                        evidence={"missing": missing, "season": season_key},
                    )
                    continue

                ck = f"mikanfeed:{mid}"
                got = cache.get_llm(ck)
                if got is None:
                    try:
                        got = {"items": _feed_items(mid)}
                    except Exception as e:
                        ctx.log(f"[episode-available] 拉 feed 失败 {mid}: {e}")
                        continue
                    cache.put_llm(ck, got)
                items = got.get("items") or []

                # 按集号归拢候选
                by_ep: dict[int, list[dict]] = {}
                for it in items:
                    n = _episode_of(it["title"])
                    if n is not None:
                        by_ep.setdefault(n, []).append(it)

                for ep in missing[:MAX_PER_SHOW]:
                    cands = by_ep.get(ep) or []
                    if not cands:
                        continue
                    best, scored = preferences.pick_best(cands, rules)
                    if best is None:
                        # 有人发了但没一个合格——报出来，别悄悄跳过
                        yield Finding(
                            rule=self.id, kind=self.kind, severity="minor",
                            summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                     f"已有 {len(cands)} 个发布，但都不含中文字幕，暂不抓取"),
                            show=show.dir_name,
                            evidence={"season": season_key, "episode": ep,
                                      "candidates": [c["title"][:110] for c in cands]},
                        )
                        continue
                    verdict = next(v for c, v in scored if c is best)
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                 f"可抓取（{len(cands)} 个候选中选 {verdict.why()}）"),
                        show=show.dir_name,
                        evidence={"season": season_key, "episode": ep,
                                  "chosen": best["title"][:140],
                                  "verdict": verdict.why(),
                                  "rejected": [f"{v.why()} | {c['title'][:90]}"
                                               for c, v in scored if c is not best][:6]},
                        action=Action(
                            op="grab_episode",
                            args={"url": best["url"], "title": best["title"],
                                  "show_dir": str(show.dir_path),
                                  "season": int(season_key), "episode": ep,
                                  "bangumi_id": sc.bangumi_id},
                            note="加入 qBittorrent 并把该集写进 sidecar 的 have 清单",
                        ),
                    )


GRAB_DETECTORS = [EpisodeAvailableDetector]
