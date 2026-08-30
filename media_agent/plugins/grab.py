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
from datetime import date, timedelta
from typing import Iterable

from .. import preferences
from ..cache import Cache
from ..kernel import Action, Context, Finding, LibraryState
from ..sidecar import load as load_sidecar, save as save_sidecar
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
    """整个番组页的 RSS（不带 subgroupid 即为全字幕组），按发布时间倒序。

    连 `<pubDate>` 一起取——它是判断"这个发布到底属于哪一季"的唯一线索，
    见 `_plausible_for` 的注释。Mikan 把它放在 `<torrent>` 子节点里，
    形如 `2026-08-22T22:33:49.61`，只取日期部分够用。
    """
    body = _http_get(f"{MIKAN}/RSS/Bangumi?bangumiId={bangumi_id}")
    out: list[dict] = []
    for it in re.findall(r"<item>(.*?)</item>", body, re.S):
        m = re.search(r"<title>(.*?)</title>", it, re.S)
        u = re.search(r'<enclosure[^>]*url="([^"]+)"', it)
        if not (m and u):
            continue
        d = re.search(r"<pubDate>\s*(\d{4}-\d{2}-\d{2})", it)
        out.append({"title": html.unescape(m.group(1)).strip(),
                    "url": html.unescape(u.group(1)).strip(),
                    "pub": d.group(1) if d else ""})
    return out


# 发布可以早于播出（抢先版、跨时区），但早不了太多。放宽到 7 天是刻意的：
# 这道闸只为拦"隔了好几季的同集号"，不该去管抢先党。
PREAIR_SLACK_DAYS = 7


def _plausible_for(item: dict, air: str) -> bool | None:
    """这个发布可能是 `air` 那天播的那一集吗？None = 信息不足，无从判断。

    背景：集号在跨季时会重复。《入间同学入魔了！》S03E19 与 S04E19 在
    发布标题里都写作 `- 19`，只看集号无法区分，抓取器曾把 2023 年发布的
    S03E19 当成 2026 年的 S04E19 抓下来并归档到第四季——错了 1274 天。

    番组页本身不区分季（Mikan 的番组命名还常年错位：标"第三季"的页面装的是
    第一季），所以季别只能靠时间反推：**发布时间远早于播出时间的，
    必定是别季的同集号。**

    缺 pubDate 的按 None 返回——不淘汰，但由调用方排到后面去，
    宁可要一个来路不明的，也不要一个明确错季的。
    """
    pub = item.get("pub") or ""
    if not (pub and air):
        return None
    try:
        return date.fromisoformat(pub) >= date.fromisoformat(air) - timedelta(
            days=PREAIR_SLACK_DAYS)
    except ValueError:
        return None


def _resolve_mikan_id(sc, show, cache) -> str | None:
    """找这部番在 Mikan 上的番组 id，找到后**写回 sidecar**，省得每轮再搜。

    解析顺序是有讲究的：
    1. sidecar 里已经存了 —— 直接用，这是落盘的意义
    2. 从已有的 rss_link 里抠 —— 番组式链接里就带着 bangumiId，
       这是**已被验证过的**映射（订阅确实从它拿到过内容）
    3. 才轮到按标题搜 —— 最不可靠：Mikan 的番组命名常年错位
       （《入间同学入魔了！》标"第三季"的页面装的是第一季），
       按标题搜到的 id 只能算猜测

    落盘用 try 包住：sidecar 写不进去（只读挂载之类）不该让检测失败，
    大不了下一轮再搜一次。
    """
    if getattr(sc, "mikan_id", None):
        return str(sc.mikan_id)

    found = None
    for s in sc.sources:
        m = re.search(r"bangumiId=(\d+)", s.get("rss_link") or "")
        if m:
            found = m.group(1)
            break
    if found is None:
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
                found = ids[0]
                break
    if found is None:
        return None

    sc.mikan_id = str(found)
    try:
        save_sidecar(show.dir_path, sc)
    except OSError:
        pass
    return sc.mikan_id


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
                air_of = {e["episode_number"]: e["air_date"] for e in eps
                          if e.get("air_date")}
                aired = {n for n, d in air_of.items() if d <= today}
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
                    raw = by_ep.get(ep) or []
                    if not raw:
                        continue

                    # 先按播出日期把明显错季的剔掉，再交给偏好打分。
                    # 顺序很重要：pick_best 同分时取靠前的那个，所以把
                    # "时间对得上"的排在前、"缺时间"的排在后，就等于给
                    # 来路不明的候选降了权——同分时永远选有据可查的那个。
                    ok, unknown, wrong_season = [], [], []
                    for it in raw:
                        v = _plausible_for(it, air_of.get(ep, ""))
                        (ok if v is True else
                         unknown if v is None else wrong_season).append(it)
                    cands = ok + unknown
                    if not cands:
                        if wrong_season:
                            yield Finding(
                                rule=self.id, kind=self.kind, severity="minor",
                                summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                         f"只搜到 {len(wrong_season)} 个明显属于别季的同集号"
                                         f"发布，全部跳过"),
                                show=show.dir_name,
                                evidence={"season": season_key, "episode": ep,
                                          "air_date": air_of.get(ep, ""),
                                          "rejected_by_date":
                                              [f"{c.get('pub','?')} | {c['title'][:90]}"
                                               for c in wrong_season][:6]},
                            )
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
                                      "candidates": [c["title"][:110] for c in cands],
                                      "rejected_by_date": len(wrong_season)},
                        )
                        continue
                    verdict = next(v for c, v in scored if c is best)
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                 f"可抓取（{len(cands)} 个候选中选 {verdict.why()}）"),
                        show=show.dir_name,
                        evidence={"season": season_key, "episode": ep,
                                  "air_date": air_of.get(ep, ""),
                                  "chosen": best["title"][:140],
                                  "chosen_pub": best.get("pub", ""),
                                  "rejected_by_date": len(wrong_season),
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
