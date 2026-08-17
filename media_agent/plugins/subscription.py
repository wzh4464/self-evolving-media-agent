"""订阅健康度检测器。

这两条规则处理的是**缺席类问题**——本该存在却没有的东西。
自演进机制推导不出它们：残留检测遍历的是已存在的文件，而缺集没有文件可遍历；
DSL 词表也只有文件维度，表达不了"订阅是否还能匹配上"和"某集是否已播出"。
详见 `.agents/notes/implemented/architecture/2026-08-17-what-evolution-cannot-derive.md`。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from ..cache import Cache
from ..kernel import Action, Context, Finding, LibraryState
from ..naming import VIDEO_EXTS

# 季番判定：这一季本身开播于多少天内。
# 用季的开播日期而不是集数阈值——常年连载番（哆啦A梦 1464 集）的第一季
# 开播于十几年前，靠这条自然排除；集数阈值则会被 TMDB "多 cour 合并成一季"骗到。
SEASONAL_WINDOW_DAYS = 300


def _fetch_rss_titles(url: str) -> list[str]:
    """拉 RSS 并取出条目标题。

    rss_link 里存的可能是未编码的中文，也可能已是 %XX 编码。
    对已编码的再编一次会变成 %25XX，请求必然返回空——这个坑踩过。
    """
    try:
        url.encode("ascii")
        safe = url
    except UnicodeEncodeError:
        parts = urllib.parse.urlsplit(url)
        safe = urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, urllib.parse.quote(parts.path),
            urllib.parse.quote(parts.query, safe="=&+%"), ""))
    req = urllib.request.Request(safe, headers={"User-Agent": "media-agent/0.1"})
    body = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    return re.findall(r"<title>(.*?)</title>", body, re.S)[1:]


def _longest_common(a: str, b: str) -> str:
    """最长公共子串。用来从实际发布标题里挖出稳定可匹配的片段。"""
    if not a or not b:
        return ""
    prev = [0] * (len(b) + 1)
    best = end = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, end = cur[j], i
        prev = cur
    return a[end - best:end].strip(" /,")


def _patterns_of(b: dict) -> list[str]:
    pats = [b["title_raw"]] if b.get("title_raw") else []
    try:
        al = json.loads(b.get("title_aliases") or "[]")
        if isinstance(al, list):
            pats.extend(a for a in al if a)
    except (json.JSONDecodeError, TypeError):
        pass
    return pats


# ---------------------------------------------------------------------------
class TitleMatchBrokenDetector:
    """订阅标题匹配失效 —— 最隐蔽的一类故障。

    AutoBangumi 认领种子靠 `title_raw`（+ title_aliases）对种子名做**纯子串匹配**
    （`module/database/bangumi.py::match_torrent` 里的 `if pattern in torrent_name`）。
    字幕组一旦改动标题，匹配就永久失效，而**外部看不出任何异常**：
    RSS 状态仍是 healthy、刷新仍返回 success、日志里一行错误都没有，就是一集不下。

    实测三种改动方式都会触发（各对应一部番）：
    - 中间插入新别名：`A / B` → `A / 新别名 / B`（尖帽子的魔法工坊）
    - 罗马音整体改写：`Sake o Tsugu` → `Yoeru Sugata wa Yuri no Hana`（上伊那牡丹）
    - 发布名里罗马音被截断（超超超超超喜欢你的100个女朋友）

    修复靠推导新别名写进 `title_aliases`：先按 " / " 拆分，再退到最长公共子串，
    最后兜底用 official_title。三段依次尝试，覆盖上述三种情况。
    """
    id = "title-match-broken"
    kind = "subscription_broken"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not state.bangumi_rows:
            return
        cache = Cache(ctx.config.cache_db)

        for b in state.bangumi_rows:
            url = b.get("rss_link") or ""
            if not url or b.get("deleted"):
                continue

            ck = f"rss:{url}"
            cached = cache.get_llm(ck)          # 复用 llm 表做通用短期缓存
            if cached is not None:
                titles = cached.get("titles", [])
            else:
                try:
                    titles = _fetch_rss_titles(url)
                except Exception as e:
                    ctx.log(f"[title-match] RSS 拉取失败 {b.get('official_title')}: {e}")
                    continue
                cache.put_llm(ck, {"titles": titles})
            if not titles:
                continue

            pats = _patterns_of(b)
            if any(p in t for t in titles for p in pats):
                continue                        # 还能匹配上，健康

            raw = b.get("title_raw") or ""
            # 1) 按 " / " 拆——解决"中间插入新别名"
            good = [f.strip() for f in re.split(r"\s*/\s*", raw)
                    if f.strip() and any(f.strip() in t for t in titles)]
            # 2) 最长公共子串——解决"罗马音被截断/改写但保留前缀"
            if not good:
                cand = _longest_common(raw, titles[0])
                if (cand and len(cand) >= 6
                        and sum(cand in t for t in titles) >= len(titles) * 0.6):
                    good = [cand]
            # 3) 兜底用中文官方名——它通常稳定出现在发布标题里
            if not good:
                ot = (b.get("official_title") or "").strip()
                if ot and sum(ot in t for t in titles) >= len(titles) * 0.6:
                    good = [ot]

            base = {"title_raw": raw, "rss_sample": titles[0][:100],
                    "rss_item_count": len(titles), "current_patterns": pats}

            if good:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="critical",
                    summary=(f"订阅「{b.get('official_title')}」标题匹配已失效"
                             f"（静默停摆，RSS 仍报 healthy），可用别名 {good}"),
                    show=b.get("official_title", ""),
                    evidence={**base, "derived_aliases": good},
                    action=Action(op="fix_title_aliases",
                                  args={"bangumi_id": b["id"], "aliases": good},
                                  note="写入别名后需清理卡住的 torrent 记录再刷新，顺序不能反"),
                )
            else:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="critical",
                    summary=(f"订阅「{b.get('official_title')}」标题匹配已失效，"
                             f"且推导不出可靠别名，需人工设定"),
                    show=b.get("official_title", ""), evidence=base,
                )


# ---------------------------------------------------------------------------
class IncompleteSeasonDetector:
    """当季/上季番剧缺集。

    **只算已播出的集数**——否则连载中的番会被整季报成缺失。
    每集的播出日期取自 TMDB 的分季接口。
    """
    id = "incomplete-season"
    kind = "incomplete_season"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not (ctx.tmdb and ctx.tmdb.enabled):
            return
        cache = Cache(ctx.config.cache_db)
        today = date.today()
        cutoff = today - timedelta(days=SEASONAL_WINDOW_DAYS)

        for show in state.shows:
            if not show.tmdb_id:
                continue

            have: dict[int, set[int]] = defaultdict(set)
            for f in show.files:
                if f.is_incomplete:
                    continue
                if f.path.suffix.lower() not in VIDEO_EXTS:
                    continue
                m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", f.filename)
                if m:
                    have[int(m.group(1))].add(int(m.group(2)))

            for sn in sorted(have):
                if sn == 0:
                    continue
                ck = f"tmdbeps:{show.tmdb_id}:{sn}"
                cached = cache.get_llm(ck)
                if cached is not None:
                    eps = cached.get("eps", [])
                else:
                    try:
                        eps = ctx.tmdb.season_episodes(show.tmdb_id, sn)
                    except Exception:
                        continue
                    cache.put_llm(ck, {"eps": eps})

                dated = []
                for e in eps:
                    if not e.get("air_date"):
                        continue
                    try:
                        dated.append((date.fromisoformat(e["air_date"]),
                                      e["episode_number"]))
                    except ValueError:
                        pass
                if not dated:
                    continue
                # 只关心季番；常年连载番的季开播于很久以前，在此排除
                if min(d for d, _ in dated) < cutoff:
                    continue

                aired = {n for d, n in dated if d <= today}
                missing = sorted(aired - have[sn])
                if not missing:
                    continue

                upcoming = [e["air_date"] for d, n in dated
                            for e in eps if e["episode_number"] == n and d > today]
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"S{sn:02d} 缺 {len(missing)} 集（已播 {len(aired)} 集，"
                             f"已有 {len(have[sn] & aired)} 集）：{missing[:10]}"),
                    show=show.dir_name,
                    evidence={"season": sn, "missing": missing,
                              "aired": len(aired), "have": len(have[sn] & aired),
                              "total": len(eps),
                              "next_air": upcoming[0] if upcoming else "已完结",
                              "tmdb_id": show.tmdb_id},
                    # 不给通用 action：缺集的成因各异（标题失配/死种/资源未发布），
                    # 由 title-match-broken 等更具体的规则给出对应修复
                )


# ---------------------------------------------------------------------------
class SourceAbandonedDetector:
    """订阅源已停更 —— 匹配一切正常，但字幕组自己弃坑了。

    与 `title-match-broken` 的区别：那是"组还在发但标题改了、匹配不上"，
    这是"匹配没问题，但订阅锁定的组不再发布新集"。
    表现同样隐蔽：RSS healthy、能匹配、就是集数停在某处不动。

    出处：《正后方的神威》订阅锁定 LoliHouse，而该组只发到第 2 集，
    黒ネズミたち 已经发到第 7 集、ANi 发了 3-7 集。

    判定：RSS 里的最大集号 < TMDB 已播出的最大集号，且差距 ≥2 集
    （差 1 集通常只是当周还没压制完，不算弃坑）。
    """
    id = "source-abandoned"
    kind = "source_abandoned"
    LAG_THRESHOLD = 2

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not (ctx.tmdb and ctx.tmdb.enabled) or not state.bangumi_rows:
            return
        cache = Cache(ctx.config.cache_db)
        today = date.today()
        cutoff = today - timedelta(days=SEASONAL_WINDOW_DAYS)

        # 订阅 → 番剧的反查必须走 save_path：
        # official_title 在目录被 TMDB 标题对齐后就对不上了
        # （例：订阅名仍是「正后方的神威」，目录已是「从后面来的神威先生」）
        by_dir = {s.dir_name: s for s in state.shows}
        by_title = {s.official_title: s for s in state.shows}

        def _show_of(b: dict):
            sp = b.get("save_path") or ""
            if sp:
                parts = Path(sp).parts
                root = ctx.config.media_root.name
                if root in parts:
                    i = parts.index(root)
                    if i + 1 < len(parts) and parts[i + 1] in by_dir:
                        return by_dir[parts[i + 1]]
            ot = b.get("official_title", "")
            return by_title.get(ot) or by_dir.get(ot)

        for b in state.bangumi_rows:
            if b.get("deleted") or not b.get("rss_link"):
                continue
            show = _show_of(b)
            if not show or not show.tmdb_id:
                continue

            ck = f"rss:{b['rss_link']}"
            cached = cache.get_llm(ck)
            if cached is not None:
                titles = cached.get("titles", [])
            else:
                try:
                    titles = _fetch_rss_titles(b["rss_link"])
                except Exception:
                    continue
                cache.put_llm(ck, {"titles": titles})
            if not titles:
                continue
            # 匹配不上是另一条规则的事，这里只看能匹配上的
            pats = _patterns_of(b)
            matched = [t for t in titles if any(p in t for p in pats)]
            if not matched:
                continue

            rss_eps = set()
            for t in matched:
                m = re.search(r"[-\[]\s*(\d{1,3})(?:v\d)?\s*[\]\[（(]", t)
                if m:
                    rss_eps.add(int(m.group(1)))
            if not rss_eps:
                continue

            season = int(b.get("season") or 1)
            ck2 = f"tmdbeps:{show.tmdb_id}:{season}"
            cached2 = cache.get_llm(ck2)
            if cached2 is not None:
                eps = cached2.get("eps", [])
            else:
                try:
                    eps = ctx.tmdb.season_episodes(show.tmdb_id, season)
                except Exception:
                    continue
                cache.put_llm(ck2, {"eps": eps})

            dated = []
            for e in eps:
                if e.get("air_date"):
                    try:
                        dated.append((date.fromisoformat(e["air_date"]),
                                      e["episode_number"]))
                    except ValueError:
                        pass
            if not dated or min(d for d, _ in dated) < cutoff:
                continue
            aired = [n for d, n in dated if d <= today]
            if not aired:
                continue

            lag = max(aired) - max(rss_eps)
            if lag < self.LAG_THRESHOLD:
                continue

            yield Finding(
                rule=self.id, kind=self.kind, severity="critical",
                summary=(f"订阅源已停更：{b.get('group_name') or '当前源'} 最新只到 "
                         f"第{max(rss_eps)}集，而第{max(aired)}集已播出（落后 {lag} 集）"),
                show=show.dir_name,
                evidence={"bangumi_id": b["id"], "group": b.get("group_name"),
                          "rss_max_episode": max(rss_eps),
                          "aired_max_episode": max(aired), "lag": lag,
                          "rss_link": b["rss_link"],
                          "hint": "需换用其他仍在更新的字幕组"},
                # 换源涉及画质/字幕语言取舍，且要重建 RSS 搜索词，
                # 不做自动切换——报出来让人或后续规则决策
            )


SUBSCRIPTION_DETECTORS = [TitleMatchBrokenDetector, SourceAbandonedDetector,
                          IncompleteSeasonDetector]
