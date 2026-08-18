"""订阅健康度检测器。

这些规则处理的是**缺席类问题**——本该存在却没有的东西。
自演进机制推导不出它们：残留检测遍历的是已存在的文件，而缺集没有文件可遍历；
DSL 词表也只有文件维度，表达不了"订阅是否还能匹配上"和"某集是否已播出"。
详见 `.agents/notes/implemented/architecture/2026-08-17-what-evolution-cannot-derive.md`。
"""
from __future__ import annotations

import html
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


# --- Mikan 番组页交叉核对 -----------------------------------------------------
# 用途见 SourceAbandonedDetector：判断"feed 安静了"到底是字幕组弃坑，
# 还是订阅锁定的**搜索式 RSS** 因字幕组改了发布名而失效。
MIKAN = "https://mikanani.me"


def _http_get(url: str, timeout: float = 25.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "media-agent/0.1"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _mikan_ids(url: str) -> tuple[str, str] | None:
    """从 `RSS/Bangumi?bangumiId=..&subgroupid=..` 里取出两个 id；搜索式链接返回 None。"""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    bid = (q.get("bangumiId") or [""])[0]
    gid = (q.get("subgroupid") or [""])[0]
    return (bid, gid) if bid and gid else None


def _mikan_search_ids(keyword: str, limit: int = 4) -> list[str]:
    """按关键词搜 Mikan，返回命中的番组 id。

    同一部番可能有多个番组条目（Mikan 按 cour 拆，第二季是独立番组），
    所以要全部返回、逐个探，不能只取第一个。
    """
    body = _http_get(f"{MIKAN}/Home/Search?searchstr={urllib.parse.quote(keyword)}")
    out: list[str] = []
    for m in re.finditer(r"/Home/Bangumi/(\d+)", body):
        if m.group(1) not in out:
            out.append(m.group(1))
        if len(out) >= limit:
            break
    return out


def _mikan_subgroups(bangumi_id: str) -> dict[str, str]:
    """番组页上的 `{subgroupid: 字幕组名}`。"""
    body = _http_get(f"{MIKAN}/Home/Bangumi/{bangumi_id}")
    out: dict[str, str] = {}
    for m in re.finditer(
            r'<div class="subgroup-text"[^>]*id="(\d+)"[^>]*>(.*?)</div>', body, re.S):
        lines = re.sub(r"<[^>]+>", "", m.group(2)).strip().splitlines()
        if lines:
            out[m.group(1)] = html.unescape(lines[0]).strip()
    return out


def _stable_feed(bangumi_id: str, subgroup_id: str) -> str:
    """番组+字幕组式 RSS。绑的是 Mikan 的 id，字幕组改发布名也不受影响。"""
    return f"{MIKAN}/RSS/Bangumi?bangumiId={bangumi_id}&subgroupid={subgroup_id}"


def _max_episode(titles: Iterable[str]) -> int | None:
    """从发布标题里取最大集号。

    比 SourceAbandonedDetector 原本那条正则宽——要覆盖各家命名：
    `- 19 [`、`S01E19`、`[19]`、`第19集`。两侧比较必须用同一个提取器，
    否则"我方看到的最大集"和"对方看到的最大集"口径不一致，会凭空比出落后。
    """
    best: int | None = None
    for t in titles:
        for m in re.finditer(r"S\d{1,2}E(\d{1,3})", t):
            n = int(m.group(1))
            best = n if best is None else max(best, n)
        for m in re.finditer(r"第\s*(\d{1,3})\s*集", t):
            n = int(m.group(1))
            best = n if best is None else max(best, n)
        for m in re.finditer(r"[-\[]\s*(\d{1,3})(?:v\d)?\s*[\]\[（(]", t):
            n = int(m.group(1))
            if n <= 200:                     # 挡掉 1080/2160 这类分辨率数字
                best = n if best is None else max(best, n)
    return best


def _common_substring(items: list[str], min_len: int = 8) -> str:
    """一组标题的最长公共子串，用作可匹配的别名。

    从"当前认不出的那批发布名"里挖片段，它天然收敛到"字幕组前缀 + 番名"
    这段（集号之后的部分更短），既够 specific 又跨集稳定。

    **必须掐掉尾部的集号残片**：一批集号若共享前缀（14~19 都以 1 开头），
    最长公共子串会把那个 `1` 一起吞进来，得到 `... 第二季 - 1`。等第 20 集
    播出就再也匹配不上——订阅又一次静默失效，正是本规则要防的东西。
    """
    if not items:
        return ""
    cur = items[0]
    for nxt in items[1:]:
        cur = _longest_common(cur, nxt)
        if len(cur) < min_len:
            return ""
    cur = cur.strip()
    trimmed = re.sub(r"[\s\-–—_/|\[(]*\d+\s*$", "", cur).rstrip(" -–—_/|[(第")
    if len(trimmed) >= min_len:
        cur = trimmed
    return cur if len(cur) >= min_len else ""


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

    ------------------------------------------------------------------
    **落后 ≠ 弃坑**：还有第三种可能，光看订阅自己的 feed 分辨不出来。

    订阅链接若是**搜索式**（`RSS/Search?searchstr=ANi+You+and+I+Are+Polar+
    Opposites+CHT+Baha+1080P`），等于把字幕组当时的发布名写死进了查询。
    字幕组一改名，这个 feed 就再也搜不到新集——安静下来的样子和弃坑一模一样：
    RSS 仍报 healthy、旧条目仍能匹配、集数就是不涨。

    实测出处：《正相反的你与我》。ANi 从第 14 集起把英文名由
    `You and I Are Polar Opposites` 换成 `Seihantai na Kimi to Boku S02`，
    订阅从此停在第 13 集。本规则当时报的是"ANi 已停更"——**结论是错的**，
    ANi 一直在发，只是搜不到了。断点精确落在 13→14，与改名时点吻合。

    所以下结论前必须交叉核对 Mikan 番组页上**同一个字幕组**的稳定 feed：
    - 对方集号更高  → 是搜索式查询失效，可自动修（换成番组式链接 + 补别名）
    - 对方也停在同处 → 才是真弃坑，换源涉及画质/字幕取舍，只报不改
    ------------------------------------------------------------------
    """
    id = "source-abandoned"
    kind = "source_abandoned"
    STALE_KIND = "stale_rss_query"
    LAG_THRESHOLD = 2
    MAX_CANDIDATES = 4          # 每部番最多探几个 Mikan 番组条目，控制网络开销

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

            # 下结论前先分辨：字幕组真弃坑，还是只是订阅这条 feed 看不到了
            alt = self._stable_source(b, titles, pats, cache, ctx)
            if alt:
                feed, alt_max, alias = alt
                extra = ("，同时补入别名以便认领新发布名" if alias else "")
                yield Finding(
                    rule=self.id, kind=self.STALE_KIND, severity="critical",
                    summary=(f"订阅的 RSS 链接已失效（**不是**弃坑）："
                             f"{b.get('group_name')} 仍在更新到第{alt_max}集，"
                             f"而这条订阅只看得到第{max(rss_eps)}集{extra}"),
                    show=show.dir_name,
                    evidence={"bangumi_id": b["id"], "group": b.get("group_name"),
                              "subscribed_max_episode": max(rss_eps),
                              "group_actual_max_episode": alt_max,
                              "aired_max_episode": max(aired),
                              "old_rss_link": b["rss_link"],
                              "new_rss_link": feed,
                              "derived_alias": alias},
                    action=Action(op="repoint_rss",
                                  args={"bangumi_id": b["id"], "rss_link": feed,
                                        "aliases": ([*pats, alias] if alias else pats)},
                                  note="番组式链接绑 Mikan id，字幕组再改发布名也不受影响"),
                )
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
                          "hint": "已确认该组在 Mikan 番组页上也没有更新，是真停更；"
                                  "需换用其他仍在更新的字幕组"},
                # 换源涉及画质/字幕语言取舍，且要重建 RSS 搜索词，
                # 不做自动切换——报出来让人或后续规则决策
            )

    def _stable_source(self, b: dict, sub_titles: list[str], pats: list[str],
                       cache, ctx) -> tuple[str, int, str] | None:
        """查同一字幕组在 Mikan 番组页上的稳定 feed，看它是否比订阅看到的更新。

        返回 `(番组式链接, 该组实际最大集号, 建议补的别名)`；确认订阅这条链接
        失效时才返回，其余情况（含"对方也停在同处"这种真弃坑）返回 None。

        为什么即使订阅已经是番组式链接也要搜：Mikan 按 cour 拆番组，第二季是
        **独立的番组条目**。订阅钉在第一季那个 id 上时，链接本身没坏，但它
        永远看不到第二季——症状同样是"集数不涨"。所以一律按标题搜同名番组，
        把所有候选都探一遍取最大。
        """
        group = (b.get("group_name") or "").strip()
        if not group:
            return None

        # 与对方同口径重算我方最大集号。原来的 rss_eps 用的是较窄的正则，
        # 两侧口径不一致会凭空比出"落后"。
        sub_max = _max_episode(sub_titles)
        if sub_max is None:
            return None

        candidates: list[str] = []
        own = _mikan_ids(b.get("rss_link") or "")
        if own:
            candidates.append(own[0])
        for kw in (b.get("official_title"), b.get("title_raw")):
            kw = (kw or "").strip()
            if not kw:
                continue
            ck = f"mikansearch:{kw}"
            hit = cache.get_llm(ck)
            if hit is None:
                try:
                    hit = {"ids": _mikan_search_ids(kw, self.MAX_CANDIDATES)}
                except Exception:
                    continue
                cache.put_llm(ck, hit)
            for i in hit.get("ids", []):
                if i not in candidates:
                    candidates.append(i)
            if len(candidates) > 1:
                break                    # 搜到就够了，别再为第二个关键词发请求

        best: tuple[str, int, list[str]] | None = None
        for bid in candidates[:self.MAX_CANDIDATES]:
            ck = f"mikansub:{bid}"
            subs = cache.get_llm(ck)
            if subs is None:
                try:
                    subs = {"groups": _mikan_subgroups(bid)}
                except Exception:
                    continue
                cache.put_llm(ck, subs)
            # 组名必须**完全相等**。模糊匹配（`group in name or name in group`）
            # 看着更宽容，实则会换掉源：《二十世纪电气目录》订阅的
            # `喵萌奶茶屋&LoliHouse` 曾因此匹配到 Mikan 上的 `喵萌奶茶屋`——
            # 那是另一条发布线（简日/繁日分开发两份，而非 LoliHouse 的
            # 简繁日内封单版本），结果 6 集拉回 12 个种子，还与已下好的
            # 5 集重复。认不准就别动，宁可退回"只报不改"。
            gid = next((g for g, name in (subs.get("groups") or {}).items()
                        if name.strip() == group), None)
            if not gid:
                continue                 # 这个番组条目里没有完全同名的字幕组

            feed = _stable_feed(bid, gid)
            if feed == (b.get("rss_link") or ""):
                continue                 # 就是订阅现在用的那条，没有新信息
            ck2 = f"rss:{feed}"
            got = cache.get_llm(ck2)
            if got is None:
                try:
                    got = {"titles": _fetch_rss_titles(feed)}
                except Exception:
                    continue
                cache.put_llm(ck2, got)
            titles = got.get("titles") or []
            mx = _max_episode(titles)
            if mx is None:
                continue
            if best is None or mx > best[1]:
                best = (feed, mx, titles)

        if not best or best[1] <= sub_max:
            return None                  # 对方没更新得更远 -> 是真弃坑，不归本分支管

        # 从"当前模式认不出的那批发布名"里挖公共子串当别名。
        # 只换链接不补别名的话，新集拉回来了却匹配不上，等于白换——实测踩过。
        unmatched = [t for t in best[2] if not any(p in t for p in pats)]
        alias = _common_substring(unmatched)
        return best[0], best[1], alias


# ---------------------------------------------------------------------------
class MissingAbTagDetector:
    """新订阅补的历史集数拿不到 `ab:` 标签 —— AutoBangumi 自身的顺序 bug。

    `module/manager/collector.py::subscribe_season` 的顺序是：

        result = await engine.download_bangumi(data)   # 先下载
        engine.bangumi.add(data)                       # 后入库，此刻才分配 id

    而打标签的代码在 `module/downloader/download_client.py::add_torrent`：

        tags = f"ab:{bangumi.id}" if bangumi.id else None

    下载发生时 `data.id` 仍是 None，于是标签为空。后果是**每次新订阅**用
    `eps_collect` 补的整季历史集数全部无标签——AB 改名时靠这个标签反查
    episode_offset，没标签就永远不会被自动改名。而外部看不出异常：种子在
    qBit 里分类、保存路径全对，只是永远停在原始发布名上。
    之后定时刷新新增的集数不受影响（那时 bangumi 已有 id）。

    只有 save_path 精确命中、**且**种子名能被该 bangumi 的匹配模式认领时才
    提议打标签。单靠 save_path 不够：episode_offset 非 0 的番若被错误认领，
    改名会把集数算错——那比不改名更难收拾。
    """
    id = "missing-ab-tag"
    kind = "subscription_untagged"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not state.bangumi_rows or not state.torrents:
            return

        by_path: dict[str, list[dict]] = defaultdict(list)
        for b in state.bangumi_rows:
            if b.get("deleted"):
                continue
            sp = (b.get("save_path") or "").rstrip("/")
            if sp:
                by_path[sp].append(b)

        for t in state.torrents:
            if t.get("category") != "Bangumi":
                continue
            if "ab:" in (t.get("tags") or ""):
                continue

            sp = (t.get("save_path") or "").rstrip("/")
            name = t.get("name") or ""
            cands = by_path.get(sp, [])
            # 双重确认：路径命中之外，种子名还得能被该番的模式认领
            owned = [b for b in cands if any(p in name for p in _patterns_of(b))]

            base = {"save_path": sp, "torrent_name": name[:120],
                    "path_candidates": [b.get("official_title") for b in cands]}

            if len(owned) == 1:
                b = owned[0]
                yield Finding(
                    rule=self.id, kind=self.kind, severity="warning",
                    summary=(f"「{b.get('official_title')}」的种子缺 ab: 标签"
                             f"（AB subscribe 顺序 bug），不补则永远不会被自动改名"),
                    show=b.get("official_title", ""),
                    torrent_hash=t.get("hash", ""),
                    evidence={**base, "bangumi_id": b["id"],
                              "episode_offset": b.get("episode_offset")},
                    action=Action(op="retag",
                                  args={"torrent_hash": t.get("hash", ""),
                                        "tags": f"ab:{b['id']}"},
                                  note="补上标签后，AB 在该种子下载完成时才会改名"),
                )
            elif owned:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="warning",
                    summary=(f"种子缺 ab: 标签，且同一保存路径下有 {len(owned)} 个订阅"
                             f"都能认领，无法确定归属，需人工指定"),
                    torrent_hash=t.get("hash", ""),
                    evidence={**base,
                              "owned_by": [b["id"] for b in owned]},
                )
            # 路径匹配不到任何订阅的种子多半是手动添加的，不报——避免噪音


SUBSCRIPTION_DETECTORS = [TitleMatchBrokenDetector, SourceAbandonedDetector,
                          IncompleteSeasonDetector, MissingAbTagDetector]
