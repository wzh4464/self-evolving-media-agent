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
import json
import re
from datetime import date, timedelta
from typing import Iterable

from .. import abmode, abrow, preferences
from ..cache import Cache, FEED_TTL, LOOKUP_TTL, season_episodes
from ..claims import fold
from ..kernel import (Action, Context, Finding, LibraryState, episode_of_file,
                      tmdb_groups)
from ..naming import (SPECIAL_RE, declared_seasons, offset_episode, parse_episode, parse_pin,
                      season_of_dir, slot_in_season)
from ..sidecar import load as load_sidecar
from ..sidecar import load_checked as load_sidecar_checked
from .subscription import (MIKAN, _disk_episodes, _http_get,
                           _mikan_search_ids, is_seasonal, rss_items)

# 单轮为一部番（每一季）最多提议抓几集，防止新订阅时一次刷屏。在**番组页上有候选**的集里数（没抓成要报的另数一份），
# 不在全部缺的集里数——页上没有的前几集不能把后面有的挡住
MAX_PER_SHOW = 6

# 一集播出多少天之后，番组页上还一个发布都没有才报（`episode_not_released`）。字幕组一般一两天内出；
# 在这之前是正常的等待，每轮都报只会把真问题淹掉。
NO_RELEASE_GRACE_DAYS = 3

# 订阅着的季不是季度番时（`is_seasonal`：上百集的连载、一档连播超过 300 天）只看最近这么多天里播的集——AB 的口径是订阅
# 之后发布的它都下；以前这种季订阅着也一集不抓、一声不吭（2026-09-27 审查）。补老集是人的事
SUBSCRIBED_RECENT_DAYS = 30

# 上一次为（这部番, 这一季）选中的番组页记多久。它只是下一轮候选里排第一的那个、每轮照样按播出日期
# 重新打分，所以放得久一点无妨；过期了也只是回到"按 rss_link / 标题重新搜"。
PICK_TTL = 90 * 86400

# feed 条目的结构版本。**改动 `_feed_items` 返回的字段就必须 +1**，
# 否则旧缓存会以缺字段的形态喂给新逻辑。
FEED_SCHEMA = "v2"


def _episode_of(title: str) -> int | None:
    """从发布标题里取集号。薄封装，真正的解析在 `naming.parse_episode`。

    这里曾经是独立的第二实现（四条自己的正则）。2026-09-07 在 2785 个真实
    名字上对比，两者分歧 15 处，**全是这份实现错**：
      `20 Seiki Denki Mokuroku - 01` → 20（把片名里的 20 当集号）
      `Isekai Quartet 3 - 08`        → 3（把季号当集号）
      `NUKITASHI … - EP04`           → None（EP 前缀不认）
      `Despicable Me 4 (2024).mp4`   → 4（电影凭空生出集号）
    而抓取决策正是靠这个函数判断"这一集有没有"的。
    """
    return parse_episode(title)[1]


def _inflight(ctx: Context, show, by_hash: dict, replace_dead: bool = True) -> dict[int, set[int]]:
    """已经有种子在下、只是还没下完的集：`{季号: {集号…}}`。

    `have` 只认下完的——这是对的，没下完就还有换源的余地。但**已经在下的
    不该无条件重复抓**：新种子会写向同一个目标路径，两个种子抢同一个文件，
    谁也校验不过（见 `colliding-torrent`，本库已发生三次）。

    `replace_dead=False`（抓取模式，`grabmode`）：死种也算在下、不放行换源——摘死种不在抓取模式里做，放行了就是
    新旧两个种子抢一个集位；换源留给 6 小时的 `run`（那一轮 dead-torrent 同批摘掉旧种子）。

    例外是停滞太久的：Re:Zero 的 E55–E58 卡在 42–92%、全网 seeds=0，
    再等下去也没有意义，这时就该换源。所以用停滞时长（`dead_torrent_hours`）
    作闸——还在动的别打扰，动不了的才换。

    "动不了"的判据与 dead-torrent **完全同一个**（`builtin.droppable_dead`）：
    放行换源的这一轮，旧种子必须同批被摘掉，否则新种子被改到同一个集位名上，
    两个种子抢一个文件。以前这里看 `now - added_on`，而死种已改按"最后一次活着"
    计时：72 小时前加入、10 小时前还在收数据的种子被放行、却不算死，新旧并存，
    旧种子只要还在给别人传分片就永远等不到被摘（2026-09-26 审查复现）。
    """
    import time

    from .builtin import droppable_dead, recorded_slot
    now = time.time()
    out: dict[int, set[int]] = {}
    for f in show.files:
        if not f.is_incomplete:
            continue
        t = by_hash.get(f.torrent_hash) or {}

        # 刚加进来的种子还叫着原始发布名，`SxxExx` 要等改名规则跑过才有。
        # 只认改名后的名字，就等于"抓下来到改完名之间"这一整段时间里
        # 这一集是不设防的——同一轮 run 里抓取比改名先跑，下一轮就会再抓一次。
        # 口径与 `have_episodes` 相同（critic N14）：先认记下来的集位（`ma:` 钉子、出处账本），
        # 再认规范名，再按发布名解析——以前规范名压过钉子，被别人按错口径改成 `S01E03` 的在下文件
        # 算成第 3 集。
        key = (recorded_slot(f, show) or parse_pin(t.get("tags") or "")
               or episode_of_file(f, allow_release_name=False))
        if key:
            sn, ep = key
        else:
            ep = _episode_of(f.torrent_name or f.filename)
            if ep is None:
                continue
            sd = season_of_dir(f.season_dir or "")
            sn = sd if sd is not None else 1   # Season 0 是 0，不能用 `or 1`

        if replace_dead and t and droppable_dead(ctx, t, now):
            continue        # 死种：本轮 dead-torrent 会摘掉它，放行换源
        out.setdefault(sn, set()).add(ep)
    return out


def _feed_items(bangumi_id: str) -> list[dict]:
    """整个番组页的 RSS（不带 subgroupid 即为全字幕组），按发布时间倒序。

    连 `<pubDate>` 一起取——它是判断"这个发布到底属于哪一季"的唯一线索，
    见 `_plausible_for` 的注释。Mikan 把它放在 `<torrent>` 子节点里，
    形如 `2026-08-22T22:33:49.61`，只取日期部分够用。
    """
    body = _http_get(f"{MIKAN}/RSS/Bangumi?bangumiId={bangumi_id}")
    out: list[dict] = []
    for title, it in rss_items(body):
        u = re.search(r'<enclosure[^>]*url="([^"]+)"', it)
        if not u:
            continue
        d = re.search(r"<pubDate>\s*(\d{4}-\d{2}-\d{2})", it)
        out.append({"title": title,
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


def _season_fit(items: list[dict], air: list[str]) -> float:
    """这个番组页的发布时间，和这一季的播出时间对得上吗？返回 0..1。

    Mikan 的番组页**不区分季，命名还常年错位**：入间同学标"第三季"的页面
    装的是第一季，标"第二季"的装第三季。所以既不能信页面名字，
    也不能信标题里的"第 N 季"——桜都把 TMDB 的第四季叫"第3季"。

    唯一可靠的锚点是时间：一季的发布集中在它播出的那几个月。
    拿页面里条目的 pubDate 和 TMDB 给的播出日期比，对得上的比例就是分数。
    """
    if not items or not air:
        return 0.0
    try:
        lo = min(date.fromisoformat(a) for a in air) - timedelta(days=PREAIR_SLACK_DAYS)
        hi = max(date.fromisoformat(a) for a in air) + timedelta(days=365)
    except ValueError:
        return 0.0
    ok = 0
    for it in items:
        pub = it.get("pub")
        if not pub:
            continue
        try:
            if lo <= date.fromisoformat(pub) <= hi:
                ok += 1
        except ValueError:
            pass
    return ok / len(items)


_SPECIAL_RE = SPECIAL_RE


def _declared_seasons(title: str) -> set[int]:
    """发布标题里明写的季号（`naming.declared_seasons`：按 ` / ` 分段各认一次）。"""
    return declared_seasons(title)


def _slot_in_season(title: str, n: int, target: int,
                    offsets: dict[int, int]) -> tuple[int | None, str]:
    """集号为 `n` 的这个发布落在目标季的第几集（`naming.slot_in_season`，出处账本读集位用同一处）。"""
    return slot_in_season(title, n, target, offsets)


def _feed_cached(mid: str, cache) -> list[dict]:
    ck = f"mikanfeed:{FEED_SCHEMA}:{mid}"
    got = cache.get_llm(ck, ttl=FEED_TTL)
    if got is None:
        got = {"items": _feed_items(mid)}
        cache.put_llm(ck, got)
    return got.get("items") or []


def _folded(groups: dict, ab_rows: dict) -> dict[int, dict[str, tuple[dict, object]]]:
    """重复目录（`tmdb_groups` 的 duplicate）上订阅的季，按宿主归拢：`{id(宿主): {季键: (订阅, 重复目录)}}`。订阅 = 它 sidecar
    里的 `subscriptions`，加上挂在它上面、落进那一季的 AB 有效订阅（没登记进 sidecar 之前）。宿主自己订阅的季以宿主的为准。"""
    from .adopt import subscription_of
    out: dict[int, dict[str, tuple[dict, object]]] = {}
    for g in groups.values():
        if g.get("kind") != "duplicate":
            continue
        box = out.setdefault(id(g["host"]), {})
        for d in g.get("duplicates", []):
            sc, problem = load_sidecar_checked(d.dir_path)
            subs = {} if problem else {str(k): (v if isinstance(v, dict) else {})
                                       for k, v in (sc.subscriptions or {}).items() if str(k).isdigit()}
            for r in ab_rows.get(fold(d.dir_name), []):
                subs.setdefault(str(abrow.library_season(r)), subscription_of(r))
            for k, sub in subs.items():
                box.setdefault(k, (sub, d))
    return out


def _offset_registered(show, season: int) -> bool:
    """宿主自己有没有这一季的集号偏移：sidecar 登记了（`0` 也算），或它的 AB 行落进这一季。"""
    from .builtin import _show_intent
    if str(season) in _show_intent(show)["episode_offsets"]:
        return True
    return bool(show.bangumi) and abrow.library_season(show.bangumi) == season


def _tmdb_said_no(why: str) -> bool:
    """分集表取不到的原因是 TMDB 答了"没有这一季"（HTTP 4xx，限流的 429 除外），不是连不上。"""
    m = re.search(r"HTTP (4\d\d)", why or "")
    return bool(m) and m.group(1) != "429"


def _ab_rows_by_dir(rows, media_root) -> dict[str, list[dict]]:
    """AB 的有效订阅行按番目录分组（`claims.fold` 过的目录名 → 按 id 排的行）。`show.bangumi` 只挂一行（同一个目录的
    第一行），可一部番可以有好几季的订阅。"""
    out: dict[str, list[dict]] = {}
    for row in sorted(rows or [], key=lambda r: int(r.get("id") or 0)):
        name = abrow.show_dir_name(row, media_root)
        if name:
            out.setdefault(fold(name), []).append(row)
    return out


def _pages_for(subs: dict, rows, season: int) -> list[str]:
    """库内第 `season` 季的订阅指定的番组页：sidecar 订阅的 `mikan_id`，再是 AB 里落进这一季的每条有效订阅的
    bangumiId（与 `episode_offset_for` 同一个口径：AB 行只对它落进的那一季，`abrow.library_season`）。"""
    pages = [str((subs.get(str(season)) or {}).get("mikan_id") or "")]
    pages += [abrow.mikan_id_of(r.get("rss_link")) for r in rows if abrow.library_season(r) == season]
    return [p for p in dict.fromkeys(pages) if p]


def _season_layout(ctx, cache, show, sc, keys, offset_for) -> tuple[dict[int, tuple[int, int, int | None]], dict | None]:
    """库内每一季落在 TMDB 哪一季的哪一段：`{库内季: (TMDB 季, 基数, 上界)}`——库内第 e 集 = TMDB 那一季的第 base + e 集，
    上界 None = 到那一季的最后一集。这一轮没法确定范围、不抓的季不在里面；第二项是它们的 `season_layout_mismatch` 证据
    （都确定得了为 None）。

    - TMDB 有这一季：就是它，整季。TMDB 的季列表是扫描按条目缓存 30 天的（`show.tmdb_seasons`），列表里没有的先问分集表
      （`season_episodes`，6 小时）：新一季刚上 TMDB 时列表还是旧的，以前抓到第二季的头几集、sidecar-sync 记下
      `seasons["2"]` 之后整部番停抓到缓存过期（2026-09-27 审查）。
    - TMDB 没有、登记了负的集号偏移 `-B`（`offset_for`：sidecar 的 `episode_offsets`，没登记时 AB 落进这一季的订阅行上的）：
      是 TMDB 上它之前最后一季（压平的那一季）从第 B+1 集起的一段，到下一个这样登记了的库内季为止；那一季自己的库内季只到
      第一个基数。《超超超超超喜欢你的100个女朋友》TMDB 一季 36 集、库里三季：`{"2": -12, "3": -24}` 让三季各是 12 集的一段。
      同一季里几个库内季的基数重复、或顺序与季号不一致：当成没登记。
    - TMDB 没有、没登记、盘上有集：多出来的季。它与 TMDB 那一季本身各是哪一段说不清——两套编号一比会把已有的判成缺失
      （2026-08-31 药屋 E25–E30 就是这么重下的），所以**除了登记了偏移的季，这部番这一轮都不抓**（与以前整部番停抓相同，
      只是登记了的照样抓：以前连 AB 37 的第三季也一起停了，订阅模式下就没人抓它）。
    订阅了、盘上还没有、TMDB 也没有的季照常交给后面（分集表取不到 / 是空的，由那里说）。"""
    listed = {int(x["season_number"]) for x in (show.tmdb_seasons or []) if x.get("season_number")}
    ints = sorted({int(k) for k in keys})
    if not listed:
        return {n: (n, 0, None) for n in ints}, None
    have_of = {int(k): (v or {}).get("have") or [] for k, v in (sc.seasons or {}).items() if str(k).isdigit()}
    tmdb = set(listed)
    for n in ints:
        if n > 0 and n not in tmdb:
            eps, _why = season_episodes(ctx, cache, show.tmdb_id, n)
            if eps:
                tmdb.add(n)
    views: dict[int, tuple[int, int, int | None]] = {}
    mapped: dict[int, tuple[int, int]] = {}
    extra: list[int] = []
    for n in ints:
        if n <= 0 or n in tmdb:
            views[n] = (n, 0, None)
            continue
        off = offset_for(n)
        host = max((t for t in tmdb if 0 < t < n), default=None)
        if off < 0 and host is not None:
            mapped[n] = (host, -off)
        elif have_of.get(n):
            extra.append(n)
        else:
            views[n] = (n, 0, None)
    by_host: dict[int, list[tuple[int, int]]] = {}
    for n, (h, b) in mapped.items():
        by_host.setdefault(h, []).append((b, n))
    for h, lst in by_host.items():
        lst.sort()
        bases = [b for b, _n in lst]
        if len(set(bases)) != len(bases) or [n for _b, n in lst] != sorted(n for _b, n in lst):
            for _b, n in lst:
                mapped.pop(n)
                if have_of.get(n):
                    extra.append(n)
                else:
                    views[n] = (n, 0, None)
            continue
        for i, (b, n) in enumerate(lst):
            views[n] = (h, b, lst[i + 1][0] if i + 1 < len(lst) else None)
        if h in views:
            views[h] = (h, 0, lst[0][0])
    if not extra:
        return views, None
    extra.sort()
    suggested: dict[str, int] = {}
    for n in extra:
        host = max((t for t in tmdb if 0 < t < n), default=None)
        base, ok = 0, host is not None
        for s in range(host or n, n):
            if s in mapped and mapped[s][0] == host:
                base = mapped[s][1]
            elif str(s) in suggested:
                base = -suggested[str(s)]
            if not have_of.get(s):
                ok = False
                break
            base += max(have_of[s])
        if ok:
            suggested[str(n)] = -base
    evidence = {"library_seasons": sorted(n for n, h in have_of.items() if n > 0 and h),
                "tmdb_seasons": sorted(tmdb), "extra": extra,
                "mapped": {str(n): [h, b] for n, (h, b) in sorted(mapped.items())},
                "suggested_offsets": suggested}
    return {n: v for n, v in views.items() if n in mapped}, evidence


def _pick_key(show, season: int | None) -> str:
    """选中的番组页在缓存里的键：按 TMDB id（目录改名不丢）+ 季——同一部番的不同季常在不同的番组页。"""
    return f"mikanpick:{show.tmdb_id or show.dir_name}:{'' if season is None else season}"


def _latest_pub(items: list[dict]) -> str:
    """番组页上最新一条发布的日期（ISO 字符串，没有返回空串）。"""
    return max((str(it.get("pub") or "") for it in items), default="")


def _resolve_mikan_id(sc, show, cache, air: list[str] | None = None, log=None,
                      season: int | None = None, preferred=(), recent: list[str] | None = None) -> str | None:
    """找这部番（这一季）在 Mikan 上的番组 id。

    候选的来源是有讲究的：
    0. 这一季的订阅指定的番组页（`preferred`：sidecar 订阅的 `mikan_id`、AB 里对这一季的有效订阅的 bangumiId、
       `subscribe --mikan`）——人为这一季选的，最先试
    1. 上一轮为这一季选中的（`state/` 缓存里的 `mikanpick:`）与 sidecar 里的 `mikan_id` —— 先试
    2. 从已有的 rss_link 里抠 —— 番组式链接里就带着 bangumiId，
       这是**已被验证过的**映射（订阅确实从它拿到过内容）
    3. 才轮到按标题搜 —— 最不可靠：Mikan 的番组命名常年错位
       （《入间同学入魔了！》标"第三季"的页面装的是第一季），
       按标题搜到的 id 只能算猜测

    **打分与平手**：按这一季已播集的播出日期（`air`）给每个候选打分（`_season_fit`），取最高的。TMDB 把几年的几档压平成
    一季时（Re:Zero：2016 → 2026 共 85 集），窗口框得住每一个页、全都是 1.0——以前严格的 `>` 让排在前面的赢（上一轮记住的、
    sidecar 里人很久以前填的「第二季」页 2259），这一档的 E83、E84 在 AB 订阅的 3951 上早就有了，一直没抓（2026-09-27 回放）。
    现在平手时依次比：是不是订阅指定的页 → 离缺的那几集近不近（`recent`：按缺的集的播出日期开窗打分）→ 最新一条发布的
    日期 → 候选的先后。

    **选中的记进缓存，不写 sidecar。** 以前在这里 `save_sidecar`——那是检测期：`diagnose`、
    `apply --dry-run`、演进器的影子验证都会改写媒体库里的 `.media-agent.json`，改动没有审计、回退不了
    （2026-09-26 状态测绘：生产上 5 份 sidecar 在 12:01–12:02 被改写，最后一条审计停在 11:17）。
    检测只许读媒体根；sidecar 里的 `mikan_id` 从此只由人写，这里只读它当候选。
    """
    stored = str(getattr(sc, "mikan_id", "") or "")
    key = _pick_key(show, season)
    remembered = str((cache.get_llm(key, ttl=PICK_TTL) or {}).get("id") or "")

    if isinstance(preferred, str):
        preferred = (preferred,)
    wanted = [str(p) for p in dict.fromkeys(str(p or "") for p in preferred) if p]

    # 没有播出日期可比时，只能沿用存下来的（老番、TMDB 查不到的情况）
    if (wanted or remembered or stored) and not air:
        return (wanted[0] if wanted else "") or remembered or stored

    cands = _mikan_candidates(sc, show, cache, log=log, season=season, preferred=wanted)
    if not cands:
        return None

    # 用播出日期给每个候选页面打分，取最高的。
    # 这一步是必要的：入间同学的 rss_link 是搜索式链接（没有 bangumiId），
    # 于是只能按标题搜，搜到的 2839 是**第三季**的页面——它每一集都齐、
    # 每个集号都对得上，唯独年份差了三年。不比时间就发现不了。
    # 只比前四个候选；订阅指定的页多于四个时（同一季好几条 AB 订阅）全都比
    best, best_fit, best_rank = None, 0.0, None
    for i, mid in enumerate(cands[:max(4, len(wanted))]):
        try:
            items = _feed_cached(mid, cache)
        except Exception as e:
            if log:
                log(f"[episode-available] 拉候选番组页 {mid} 的 feed 失败：{type(e).__name__}: {e}")
            continue
        fit = _season_fit(items, air)
        rank = (fit, mid in wanted, _season_fit(items, recent or []), _latest_pub(items), -i)
        if fit > 0 and (best_rank is None or rank > best_rank):
            best, best_fit, best_rank = mid, fit, rank
    if best is None or best_fit < 0.15:
        # 一个都对不上：宁可不抓，也不要从错的季里抓。
        # 返回 None 会让上层报"找不到 Mikan 番组页"，那是准确的描述。
        return None

    if best != remembered:
        cache.put_llm(key, {"id": str(best), "fit": round(best_fit, 3)})
    return str(best)


def _mikan_candidates(sc, show, cache, log=None, season: int | None = None, preferred=()) -> list[str]:
    """这部番可能对应的全部 Mikan 番组页，顺序即可信度：这一季的订阅指定的（`preferred`）、上轮为这一季选中的、
    sidecar 记着的、rss_link 里的、按标题搜到的（搜索结果走缓存）。`_resolve_mikan_id` 从中按播出日期挑一页；
    缺的集在那一页上没有候选时，抓取再按这一集的播出日期到其余页里找。"""
    stored = str(getattr(sc, "mikan_id", "") or "")
    remembered = str((cache.get_llm(_pick_key(show, season), ttl=PICK_TTL) or {}).get("id") or "")
    cands: list[str] = []
    for c in (*(str(p or "") for p in preferred), remembered, stored):
        if c and c not in cands:
            cands.append(c)
    for s in sc.sources:
        m = re.search(r"bangumiId=(\d+)", s.get("rss_link") or "")
        if m and m.group(1) not in cands:
            cands.append(m.group(1))
    for kw in [sc.canonical_title, show.official_title, *sc.aliases]:
        kw = (kw or "").strip()
        if not kw:
            continue
        ck = f"mikansearch:{kw}"
        hit = cache.get_llm(ck, ttl=LOOKUP_TTL)
        if hit is None:
            try:
                hit = {"ids": _mikan_search_ids(kw, 3)}
            except Exception as e:
                if log:
                    log(f"[episode-available] Mikan 搜索失败 {kw}：{type(e).__name__}: {e}")
                continue
            cache.put_llm(ck, hit)
        for i in (hit.get("ids") or []):
            if i not in cands:
                cands.append(i)
    return cands


# 番组页"覆盖"某一集：页上发布的时间跨度把这集的播出日期包进来。页上最早的发布可以比播出晚到 30 天（迟到的搬运组、
# 合集），最晚的发布可以比播出早到 `PREAIR_SLACK_DAYS`（抢先党）。v0.5.1 把两头的松弛写反了：第一集播出 20 天后
# 才有合集的页，覆盖不到它自己的第一集。
LATE_SLACK_DAYS = 30


def _pub_span(items: list[dict]):
    pubs = []
    for it in items:
        try:
            pubs.append(date.fromisoformat(it.get("pub") or ""))
        except ValueError:
            pass
    return (min(pubs), max(pubs)) if pubs else None


def _span_covers(span, air: str) -> bool:
    try:
        a = date.fromisoformat(air)
    except (TypeError, ValueError):
        return False
    lo, hi = span
    return lo - timedelta(days=LATE_SLACK_DAYS) <= a <= hi + timedelta(days=PREAIR_SLACK_DAYS)


class EpisodeAvailableDetector:
    """已播出、本地没有、而且此刻已经有可接受版本的集数。

    "可接受"由 preferences 判定；只在**当下**可得的候选里挑，不等更好的——
    这就是"谁先出要谁"。等不到合格版本的集数会一直报，直到有人做出来。
    """
    id = "episode-available"
    kind = "episode_grabbable"

    def __init__(self, *, replace_dead: bool = True):
        # 旧种子死了要不要放行换源（`_inflight`）。`run` 放行（同一批 dead-torrent 摘掉旧种子）；抓取模式不放行
        self.replace_dead = replace_dead

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not (ctx.tmdb and ctx.tmdb.enabled):
            return
        from .builtin import episode_offset_for

        cache = Cache(ctx.config.cache_db)
        rules = preferences.load_rules()
        today = date.today().isoformat()

        # 多个目录解析到同一个 tmdb_id 时要先分清是哪一种，见 kernel.tmdb_groups：
        #
        #   duplicate —— 同一批内容存了两份（或一份是空壳）。只认宿主，
        #                其余跳过并单独告警一次。不加这道闸的话，每个空壳目录
        #                都会以为自己缺整季、轮流去抓同一集：实测《入间同学入魔了！》
        #                的三个历史分季目录连续三天、每 6 小时抓一次 S04E19。
        #                qBittorrent 靠 infohash 挡住了重复下载，所以没造成损害——
        #                也正因如此，空转了三天没人发现。
        #
        #   volumes   —— TMDB 把多部独立作品收成一个条目（物语系列 14 个目录）。
        #                这些目录各自是完整作品，**每个都要能独立补集**，
        #                当成重复跳过就等于让其中 13 部永远不再更新。
        groups = tmdb_groups(state.shows)
        by_hash = {t["hash"]: t for t in state.torrents}
        dup_reported: set[int] = set()
        ab_rows = _ab_rows_by_dir(state.bangumi_rows, ctx.config.media_root)
        # 重复目录上的订阅并进宿主（`_folded`）：AB 按自己的标题建续作的保存路径（`旧番庚 第二季/Season 2`），与本项目按
        # TMDB 改过名的番目录（`旧番庚`）对不上；订阅那一刻 AB 一集都没补时，`create_show_dir` 建出的只是个只有档案的空壳——
        # 它被归成重复目录、抓取跳过，宿主又没有这一季的订阅，第二季永远不来（2026-09-27 审查复现）
        folds = _folded(groups, ab_rows)

        for show in state.shows:
            if show.naming_hold:
                continue                 # 标题认不准：抓来的会按退回的名字改名、落进那个分类（naming_held 另报）
            if not show.tmdb_id:
                yield from self._unidentified(ctx, show, ab_rows.get(fold(show.dir_name), []))
                continue
            g = groups.get(show.tmdb_id) or {}
            if g.get("kind") == "duplicate" and show in g.get("duplicates", []):
                host = g["host"]
                if show.tmdb_id not in dup_reported:
                    dup_reported.add(show.tmdb_id)
                    sibs = [s.dir_name for s in g["shows"]]
                    folded = {k: d.dir_name for k, (_sub, d) in sorted(folds.get(id(host), {}).items(),
                                                                         key=lambda kv: int(kv[0]))}
                    yield Finding(
                        rule=self.id, kind="duplicate_show_dir", severity="important",
                        summary=(f"{len(sibs)} 个目录装着同一批内容（TMDB "
                                 f"{show.tmdb_id}），抓取只认文件最多的"
                                 f"「{host.dir_name}」，其余会被跳过"
                                 + (f"；它们上面订阅的第 {sorted(int(k) for k in folded)} 季抓进「{host.dir_name}」"
                                    if folded else "")),
                        show=host.dir_name,
                        evidence={"tmdb_id": show.tmdb_id, "dirs": sibs,
                                  "host": host.dir_name,
                                  "duplicates": [d.dir_name for d in g["duplicates"]],
                                  "folded": folded,
                                  "hint": "多半是历史遗留的分季目录或繁简两版，应合并"},
                    )
                continue
            sc = load_sidecar(show.dir_path)
            # 要看的季：盘上有文件的（sidecar-sync 记在 `seasons` 里）+ 订阅了的（`subscriptions`：盘上一集都还没有
            # 也抓——新番、新一季以前要等 AutoBangumi 放进第一个文件）
            subs = {str(k): (v if isinstance(v, dict) else {})
                    for k, v in (sc.subscriptions or {}).items() if str(k).isdigit()}
            folded = {k: v for k, v in folds.get(id(show), {}).items() if k not in subs}
            subs = {**{k: sub for k, (sub, _d) in folded.items()}, **subs}
            rows = ab_rows.get(fold(show.dir_name), []) + [r for _sub, d in folded.values()
                                                           for r in ab_rows.get(fold(d.dir_name), [])
                                                           if str(abrow.library_season(r)) in folded]
            # 人明说要抓的季：sidecar 的订阅，或 AB 里落进这一库内季的有效订阅（`abrow.library_season`）
            subscribed = set(subs) | {str(abrow.library_season(r)) for r in rows}
            season_keys = sorted({k for k in sc.seasons if str(k).isdigit()} | set(subs), key=int)
            if not season_keys:
                continue

            # 库内季号与 TMDB 季号对不上时，`have` 是按库内编号统计的，
            # 而"缺哪几集"是按 TMDB 编号算的——两套编号一比，就会把已有的
            # 内容判成缺失。
            #
            # 实测（2026-08-31）：TMDB 把《药屋少女的呢喃》压平成单季 49 集，
            # 库里却是 S01E01-24 + S02E01-24 两个目录。key "1" 的 have 只有
            # 1..24，于是 E25-E30 被判成缺失并抓了下来——它们就是磁盘上的
            # S02E01-E06。《超超超超超喜欢你的100个女朋友》同样被重复抓了 6 集。
            #
            # 换算靠集号偏移（`episode_offsets`：库内这一季是压平的那一季从第几集起的一段），那是人（或 AB 订阅行）
            # 该决定的事：没登记的多出来的季，在配好之前宁可不抓（`_season_layout`）。2026-09-27 回放之前这里是
            # "库里有 TMDB 没有的季就整部番停抓"，建议写的是改变不了这一点的 `season_offsets`——AB 37 的第三季登记了
            # -24 也一样停着，订阅模式下就没人抓它
            def offset_for(n: int, show=show, folded=folded) -> int:
                # 并进来的季：宿主自己没登记（sidecar、落进这一季的 AB 行）时按那个重复目录的（它的 sidecar、它的 AB 行）
                owner = folded.get(str(n), (None, None))[1]
                if owner is not None and not _offset_registered(show, n):
                    return episode_offset_for(owner, n)
                return episode_offset_for(show, n)

            views, layout = _season_layout(ctx, cache, show, sc, season_keys, offset_for)
            if layout:
                sugg = layout["suggested_offsets"]
                yield Finding(
                    rule=self.id, kind="season_layout_mismatch", severity="important",
                    classified=True,
                    summary=(f"库内有 Season {layout['extra']} 而 TMDB 只有 Season {layout['tmdb_seasons']}，"
                             f"这几季没登记集号偏移——两套集号口径不一致，"
                             + (f"除了登记了偏移的 Season {sorted(int(k) for k in layout['mapped'])}，"
                                if layout["mapped"] else "")
                             + "已停止对这部番自动抓取，以免把已有的集数重下一遍。在 sidecar 的 episode_offsets 里"
                             "登记多出来的每个库内季的偏移（它在 TMDB 那一季里之前有几集就是负几"
                             + (f"；按库内各季的集数推测：{json.dumps(sugg, ensure_ascii=False)}，核对后再写"
                                if sugg else "") + "），或按 TMDB 重编排目录"),
                    show=show.dir_name, evidence=layout,
                )
                if not views:
                    continue
            inflight = _inflight(ctx, show, by_hash, replace_dead=self.replace_dead)

            disk_eps = _disk_episodes(show)

            for season_key in season_keys:
                view = views.get(int(season_key))
                if view is None:
                    continue                     # 季的编排对不上（上面报过）
                tmdb_season, base, upper = view
                ranged = bool(base or upper is not None)
                info = sc.seasons.get(season_key) or {}
                # `have` 要并上磁盘实况，不能只信 sidecar。
                #
                # 一轮 run 是"先全量诊断、再统一执行"：抓取检测器跑的时候，
                # sidecar 还是**上一轮**写的。AutoBangumi 在两轮之间下完的集，
                # 磁盘上已经有了，sidecar 里却还没有——于是抓取器认为它缺，
                # 再下一遍。2026-09-03 实测：《无职转生》S03E10 十六小时前
                # 就由 AB 下完躺在磁盘上，sidecar 的 have 仍停在 9。
                #
                # 这跟 seasonal 标记那次是同一类问题：依赖会滞后一整轮的
                # 持久化状态。凡是磁盘能直接回答的，就别问镜像。
                have = set(info.get("have") or []) | disk_eps.get(int(season_key), set())
                # 分集表走缓存（`cache.season_episodes`：在播 6 小时、播完 7 天、取不到 6 小时内不再问）。以前这里
                # 每一遍都不带缓存地问：生产 129 个 sidecar、159 个季键，一次 diagnose 43.98 秒里 31.9 秒是它
                # （runloop 调研 2026-09-26）——`run` 迭代到不动点时每次迭代都要再付一遍
                eps, why = season_episodes(ctx, cache, show.tmdb_id, tmdb_season)
                sub = season_key in subscribed
                if eps is None:
                    if not _tmdb_said_no(why):
                        ctx.log(f"[episode-available] TMDB 第 {tmdb_season} 季集表读取失败 {show.dir_name}，"
                                f"这一季这一轮不抓：{why}")
                    elif sub:
                        yield self._unserved(ctx, show, int(season_key), rows, "tmdb_no_season",
                                             f"TMDB 上没有第 {tmdb_season} 季（{why}）")
                    else:
                        ctx.log(f"[episode-available] TMDB 上没有第 {tmdb_season} 季（{why}）：{show.dir_name} 的这一季不抓")
                    continue
                if ranged:
                    # 压平的那一季里属于这个库内季的一段，按库内编号（第 base + e 集 = 库内第 e 集）
                    top = upper if upper is not None else float("inf")
                    eps = [{**e, "episode_number": int(e["episode_number"]) - base} for e in eps
                           if isinstance(e.get("episode_number"), int) and base < e["episode_number"] <= top]
                air_of = {e["episode_number"]: e["air_date"] for e in eps
                          if e.get("air_date")}

                # 常年连载番（哆啦A梦之流）不参与。
                #
                # 这里**当场从 TMDB 数据算**，不读 sidecar 里那个 `seasonal` 标记。
                # 一个 run 是"先全量诊断、再统一执行"：抓取检测器跑的时候，
                # sidecar 还是上一轮写下的。2026-08-31 改了判定口径之后，
                # 哆啦A梦的标记要到本轮 apply 才被改回 false，而抓取检测器在同一轮
                # 更早的时候已经按旧标记提议了 6 集 2005 年的内容——两轮 run 抓了
                # 两次。依赖会滞后一整轮的持久化状态，就是在给自己埋这种坑。
                dates = []
                for e in eps:
                    if e.get("air_date"):
                        try:
                            dates.append(date.fromisoformat(e["air_date"]))
                        except ValueError:
                            pass
                recent_from = ""
                if not is_seasonal(dates, len(eps), date.today()):
                    if not sub:
                        continue
                    # 订阅着的长篇 / 连播的季：只看最近播的（上面的常量）。一集都没有就是播完了，没什么可抓，不报
                    recent_from = (date.today() - timedelta(days=SUBSCRIBED_RECENT_DAYS)).isoformat()
                aired = {n for n, d in air_of.items() if recent_from <= d <= today}
                if not aired and not eps and sub:
                    yield self._unserved(ctx, show, int(season_key), rows, "tmdb_no_episodes",
                                         (f"TMDB 第 {tmdb_season} 季第 {base + 1} 集起还没有分集（这一季是压平那一季的一段）"
                                          if ranged else f"TMDB 第 {tmdb_season} 季还没有分集表"), severity="minor")
                    continue
                # 已经在下的不重复抓（除非停滞太久，见 _inflight 的注释）
                busy = inflight.get(int(season_key), set())
                missing = sorted(aired - have - busy)
                if not missing:
                    continue
                # 订阅着的季"停下来了"：播出超过宽限期、又比库里（含在下的）最新一集还新的缺集——不是订阅之前就缺着的
                # 老空缺。这些发现要进卡住检测与通知（important；`history.qualifies` 不数 minor）：订阅模式下抓取是唯一的
                # 下载者，找不到番组页 / 候选全被拒 / 没发布以前都是 minor，Re:Zero 就停在这里没人收到（2026-09-27 审查）
                grace_cut = (date.fromisoformat(today) - timedelta(days=NO_RELEASE_GRACE_DAYS)).isoformat()
                newest = max(have | busy, default=0)

                def stalled(ep: int, sub=sub, season=int(season_key), newest=newest, air_of=air_of,
                            grace_cut=grace_cut) -> bool:
                    return sub and season > 0 and ep > newest and bool(air_of.get(ep)) and air_of[ep] <= grace_cut

                preferred = _pages_for(subs, rows, int(season_key))
                mid = _resolve_mikan_id(
                    sc, show, cache, log=ctx.log, season=int(season_key),
                    air=[d for d in (air_of.get(n) for n in aired) if d],
                    preferred=preferred,
                    recent=[d for d in (air_of.get(n) for n in missing) if d])
                if not mid:
                    late = [ep for ep in missing if stalled(ep)]
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important" if late else "minor",
                        subject=f"S{int(season_key):02d}",
                        summary=(f"「{show.official_title}」缺 {len(missing)} 集"
                                 f"{missing[:8]}，但找不到 Mikan 番组页，无从抓取"
                                 + (f"（订阅着，{late[:8]} 播出已超过 {NO_RELEASE_GRACE_DAYS} 天）" if late else "")),
                        show=show.dir_name,
                        evidence={"missing": missing, "season": season_key, "stalled": late},
                    )
                    continue

                # 缓存 key 必须带结构版本。加 `pub` 字段那次没带，于是旧缓存里
                # 每个候选都没有 pub，`_plausible_for` 一律返回"无日期"，
                # 整道日期校验静默退化成不设防——代码在跑、日志正常、
                # 一条错误都不报，只是不再拦任何东西。实测因此抓了一集
                # 2023 年的第三季内容进 Season 4。
                try:
                    items = _feed_cached(mid, cache)
                except Exception as e:
                    ctx.log(f"[episode-available] 拉 feed 失败 {mid}: {e}")
                    continue

                # 按集号归拢候选。
                #
                # 番组页按分季编号发布、而库内用 TMDB 连续编号时，同一集在两边
                # 是两个数字：Re:Zero 第四季的番组页把 E80 发成「S04E14」，
                # `by_ep[80]` 于是是空的，整集被静默跳过——既不抓也不报警。
                #
                # 换算必须以**候选自己声明的季号**为准，不能拿 `season_offsets`
                # 里的偏移逐个去试：试探法会把 `S04E14`（偏移 66）也登记到
                # `39 = 14 + 25` 上，而 S0E39 那条特典播于 2021 年，日期校验
                # 只防"早于播出"、不防"晚于播出"（老番重新做种本来就晚），
                # 拦不住它——实测差点把 2026 年的第四季第 14 集当成 2021 年的特典抓下来。
                #
                # 声明的季号对不上目标季、又没有偏移换算的，**不是候选**（LAT-03）：2026-09-06 辉夜大小姐的
                # 特典位 S00E03 / E04 被抓进了「第三季 / … S3 - 03 / 04」——以前按裸集号归拢、声明的季号
                # 只用来加一个换算后的位置，原来的集号照样登记。它们记进 `off_season`，最后照样报出来。
                off_by_season = {int(k): int(v)
                                 for k, v in (sc.season_offsets or {}).items()
                                 if str(k).isdigit()}
                # 集号偏移（critic N13）：AB 37《100个女朋友》第三季的发布按连续集号编，`- 25` 是 S03E01。以前这里
                # 只认原始集号：`by_ep[25]` 有、要找的 `by_ep[1]` 空着，下面一句 `continue`——既不抓也不报，AB 一退役
                # 这部番就无声地停了。偏移与判重 / 改名 / 出处账本同一个口径（`episode_offset_for`、`offset_episode`：
                # 换算出非正数的不收，理由记进 `off_season`，照样报出来）
                ep_off = offset_for(int(season_key)) if int(season_key) else 0

                def place(it, span, tmdb_season=tmdb_season, ep_off=ep_off, off_by_season=off_by_season,
                          air_of=air_of):
                    """一条发布的（原始集号, 落在这一库内季的第几集, 不收的理由）：按 TMDB 那一季的编号认（压平的季里
                    `S01E30` 就是 TMDB 第 1 季第 30 集），再按这一库内季的集号偏移换算。主番组页与兜底的别的页同一个口径。

                    **没写季号的集号只在它所在的页覆盖这集的播出日期时才算数**（`span`：这一页的发布时间跨度）。平手时选页
                    会挑发布最新的那一页（Re:Zero 的新一档），压平的季里缺的是好几年前的老集时，那一页上没写季号的 `- 06`
                    是它自己那一档的第 6 集，不是第 6 集（合并 v0.5.1 时它的测试抓出来的）。"""
                    n = _episode_of(it["title"])
                    if n is None:
                        return None, None, ""
                    ep_here, why = _slot_in_season(it["title"], n, tmdb_season, off_by_season)
                    if ep_here is not None and ep_off:
                        shifted = offset_episode(ep_here, ep_off)
                        if shifted is None:
                            why = (f"按集号偏移 {ep_off:+d} 换算出非正数（{ep_here} → {ep_here + ep_off}），"
                                   f"说不清是哪一集")
                        ep_here = shifted
                    aired_on = air_of.get(ep_here) if ep_here is not None else None
                    if (aired_on and span and not _declared_seasons(it["title"])
                            and not _span_covers(span, aired_on)):
                        return n, None, (f"没写季号，所在番组页的发布（{span[0]}–{span[1]}）对不上这一集的播出日期"
                                         f"（{aired_on}）")
                    return n, ep_here, why

                by_ep: dict[int, list[dict]] = {}
                off_season: dict[int, list[tuple[dict, str]]] = {}
                span = _pub_span(items)
                for it in items:
                    n, ep_here, why = place(it, span)
                    if n is None:
                        continue
                    if ep_here is None:
                        off_season.setdefault(n, []).append((it, why))
                    else:
                        by_ep.setdefault(ep_here, []).append(it)

                # 订阅着的季：先给每一集找候选，再在有候选的集里数 `MAX_PER_SHOW`（抓的、没抓成要报的各数各的）。以前
                # `missing[:6]` 先截断：新订阅常常是"缺的全是已播的"，页上没有前几集（半路订的、Mikan 把两档分成两个页而
                # TMDB 是一季）时每次都只看同样的前 6 集，后面还在播的永远轮不到（2026-09-27 回放：AB 6 的 E24、E25）。
                # 没人订的季照旧只看缺的前 6 集：回放里没人订的【我推的孩子】（库里只有第一档 11 集、TMDB 压平成 35 集）
                # 放开之后要越过第二档整档的空缺，把第三档 E25–E35 抓进来——补不补老档是人的决定，不是抓取的
                window = missing if season_key in subscribed else missing[:MAX_PER_SHOW]

                # TMDB 把多季压成一季、Mikan 却每季一页：主页面上某集一个候选都没有时，去别的候选页里
                # **发布时间覆盖这集播出日期**的那些页找（2026-09-27 超百：按全季日期选中了第二季那页 3524，
                # 第 30、31 集在第三季的 3997 上，检测器静默跳过、从没报过）。按日期选页，不拿别季同集号凑数。
                # 这一季有订阅指定的页（`preferred`）时只在那几页里找：订阅指定页是人的意图，别的页"日期对得上"只是猜；
                # 订阅的页上真没有，由 `episode_not_released` 报出来（订阅着的季停下来是 important）
                need = [ep for ep in window if not by_ep.get(ep) and not off_season.get(ep)]
                looked = [mid]
                if need:
                    for pid in (preferred or _mikan_candidates(sc, show, cache, log=ctx.log, season=int(season_key))):
                        if pid in looked or len(looked) >= max(4, len(preferred)):
                            continue
                        try:
                            pitems = _feed_cached(pid, cache)
                        except Exception as e:
                            ctx.log(f"[episode-available] 拉候选番组页 {pid} 的 feed 失败：{type(e).__name__}: {e}")
                            continue
                        looked.append(pid)
                        span = _pub_span(pitems)
                        covered = {ep for ep in need if span and _span_covers(span, air_of.get(ep, ""))}
                        for it in (pitems if covered else ()):
                            n, ep_here, why = place(it, span)
                            if n is None:
                                continue
                            it = {**it, "page": pid}     # 抓到的记进出处账本的是它真正所在的页
                            if ep_here in covered:
                                by_ep.setdefault(ep_here, []).append(it)
                            elif ep_here is None and n in covered:
                                off_season.setdefault(n, []).append((it, why))

                silent: list[int] = []           # 哪一页的归拢表里都什么都没有的集：循环之后统一说
                grabs = noted = 0
                for ep in window:
                    raw = by_ep.get(ep) or []
                    elsewhere = off_season.get(ep) or []
                    if not raw and not elsewhere:
                        silent.append(ep)
                        continue
                    if grabs >= MAX_PER_SHOW and noted >= MAX_PER_SHOW:
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
                    by_season = [f"{why} | {c['title'][:90]}" for c, why in elsewhere][:6]
                    if not cands:
                        if (wrong_season or elsewhere) and noted < MAX_PER_SHOW:
                            noted += 1
                            yield Finding(
                                rule=self.id, kind=self.kind, severity="important" if stalled(ep) else "minor",
                                subject=f"S{int(season_key):02d}E{ep:02d}",
                                summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                         f"只搜到 {len(wrong_season) + len(elsewhere)} 个明显属于别季的同集号"
                                         f"发布（{len(elsewhere)} 个标的是别季 / 不是特典 / 按集号偏移换算不进来，"
                                         f"{len(wrong_season)} 个早于播出），全部跳过"),
                                show=show.dir_name,
                                evidence={"season": season_key, "episode": ep,
                                          "air_date": air_of.get(ep, ""), "mikan_id": mid,
                                          "rejected_by_season": by_season,
                                          "rejected_by_date":
                                              [f"{c.get('pub','?')} | {c['title'][:90]}"
                                               for c in wrong_season][:6]},
                            )
                        continue
                    best, scored = preferences.pick_best(
                        cands, preferences.with_requirement(
                            rules, sc.require_any,
                            name="只保留" + "/".join(sc.require_any)))
                    if best is None:
                        # 有人发了但没一个合格——报出来，别悄悄跳过
                        if noted >= MAX_PER_SHOW:
                            continue
                        noted += 1
                        yield Finding(
                            rule=self.id, kind=self.kind, severity="important" if stalled(ep) else "minor",
                            subject=f"S{int(season_key):02d}E{ep:02d}",
                            summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                     f"已有 {len(cands)} 个发布，但都未通过硬门槛"
                                     f"（{'、'.join(sorted({v.blocked_by for _c, v in scored}))}），"
                                     f"暂不抓取"),
                            show=show.dir_name,
                            evidence={"season": season_key, "episode": ep, "mikan_id": mid,
                                      "candidates": [c["title"][:110] for c in cands],
                                      "rejected_by_date": len(wrong_season),
                                      "rejected_by_season": by_season},
                        )
                        continue
                    if grabs >= MAX_PER_SHOW:
                        continue
                    grabs += 1
                    verdict = next(v for c, v in scored if c is best)
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        subject=f"S{int(season_key):02d}E{ep:02d}",
                        summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                 f"可抓取（{len(cands)} 个候选中选 {verdict.why()}）"),
                        show=show.dir_name,
                        evidence={"season": season_key, "episode": ep,
                                  "air_date": air_of.get(ep, ""), "mikan_id": best.get("page") or mid,
                                  "chosen": best["title"][:140],
                                  "chosen_pub": best.get("pub", ""),
                                  "rejected_by_date": len(wrong_season),
                                  "rejected_by_season": by_season,
                                  "verdict": verdict.why(),
                                  # 结构化的评分与候选数：执行时记进出处账本（审计只存 args，这些以前哪儿都没留下）
                                  "verdict_detail": {"acceptable": verdict.acceptable,
                                                     "score": verdict.score,
                                                     "passed": list(verdict.passed),
                                                     "penalties": list(verdict.penalties),
                                                     "blocked_by": verdict.blocked_by},
                                  "candidates": len(cands),
                                  "rejected_count": len(scored) - 1,
                                  "rejected": [f"{v.why()} | {c['title'][:90]}"
                                               for c, v in scored if c is not best][:6]},
                        action=Action(
                            op="grab_episode",
                            args={"url": best["url"], "title": best["title"],
                                  "show_dir": str(show.dir_path),
                                  "season": int(season_key), "episode": ep,
                                  "bangumi_id": sc.bangumi_id,
                                  "category": show.official_title,
                                  "official_title": show.official_title},
                            note="加入 qBittorrent 并把该集写进 sidecar 的 have 清单",
                        ),
                    )
                if silent:
                    yield from self._silent(show, int(season_key), silent, by_ep, eps, air_of, looked,
                                            ep_off, today, ranged=ranged, stalled=stalled)

    def _unidentified(self, ctx: Context, show, rows) -> Iterable[Finding]:
        """TMDB 认不出的番目录（`create_show_dir` / `media-agent subscribe` 建的、按目录名搜不到）：订阅着的季抓不了，说出来。
        以前一句 `continue`——`grab` 与 `run` 都没有任何发现（2026-09-27 审查）。没人订的照旧不报（身份由 tmdb-identity 管）。"""
        sc, problem = load_sidecar_checked(show.dir_path)
        subs = set() if problem else {str(k) for k in (sc.subscriptions or {}) if str(k).isdigit()}
        for season in sorted(subs | {str(abrow.library_season(r)) for r in rows}, key=int):
            yield self._unserved(ctx, show, int(season), rows, "no_tmdb",
                                 "TMDB 认不出这部番（按目录名 / AB 标题都没搜到，sidecar 也没有 tmdb_id）")

    def _unserved(self, ctx: Context, show, season: int, rows, reason: str, why: str, *,
                  severity: str | None = None) -> Finding:
        """订阅着的季抓不了（`subscription_unserved`）。`full` 模式下 AB 照着它的订阅下，只有 AB 订阅着的季报 minor；
        订阅模式下、或只有 sidecar 订阅（`media-agent subscribe`、新季登记）的季没有别人下，important。"""
        by_ab = any(abrow.library_season(r) == season for r in rows)
        if severity is None:
            severity = "minor" if (by_ab and abmode.ab_downloads(ctx.config)) else "important"
        if by_ab and abmode.ab_downloads(ctx.config):
            tail = "——AB 还在按它的订阅下"
        elif reason == "tmdb_no_episodes":
            tail = "——多半是还没定档，TMDB 有了分集就接着抓"
        else:
            tail = "——没有别人下：在 sidecar 里写 tmdb_id / 登记集号偏移，或改订阅"
        return Finding(
            rule=self.id, kind="subscription_unserved", severity=severity, subject=f"S{season:02d}",
            summary=f"「{show.official_title or show.dir_name}」第 {season} 季订阅着，但抓取抓不了：{why}{tail}",
            show=show.dir_name,
            evidence={"season": season, "reason": reason, "detail": why,
                      "bangumi_ids": [r.get("id") for r in rows if abrow.library_season(r) == season]})

    def _silent(self, show, season: int, silent: list[int], by_ep: dict, eps: list[dict],
                air_of: dict, pages: list[str], ep_off: int, today: str, *, ranged: bool = False,
                stalled=lambda ep: False) -> Iterable[Finding]:
        """要找的集在归拢表里什么都没有（N13 以前的 bare `continue`；主番组页 `pages[0]` 与按播出日期兜底查过的别的页
        都算上）：说出来。

        - 番组页上有编号落在这一季**之外**（比 TMDB 这一季最后一集还大）：多半是按连续集号发布、又没登记集号偏移——
          这一季一集都抓不到，报 `episode_numbering_mismatch`（important），带上按最小编号推测的偏移供人核对；
        - 否则是真的还没人发：播出超过 `NO_RELEASE_GRACE_DAYS` 天的才报 `episode_not_released`（minor；订阅着的季里比库里
          最新一集还新的——这部番停下来了——important，`stalled`），刚播的是正常的等待。"""
        last = max((int(e["episode_number"]) for e in eps if e.get("episode_number")), default=0)
        # 压平的季里的一段（`_season_layout`）：落在这一段之外的是同一季别的库内季的，不是编号对不上
        unplaced = [] if ranged else sorted(n for n in by_ep if n > last)
        if unplaced:
            guess = 1 - unplaced[0] + (ep_off or 0)
            yield Finding(
                rule=self.id, kind="episode_numbering_mismatch", severity="important",
                subject=f"S{season:02d}",
                summary=(f"「{show.official_title}」S{season} 缺 {silent}，番组页上的发布编号 "
                         f"{unplaced[0]}–{unplaced[-1]} 却落在 TMDB 第 {season} 季（{last} 集）之外——多半是按连续集号"
                         f"发布：在 sidecar 的 episode_offsets 里登记这一季的偏移（若 {unplaced[0]} 就是第 1 集："
                         f"{{\"{season}\": {guess}}}），之前这一季一集都抓不到"),
                show=show.dir_name,
                evidence={"season": season, "missing": silent, "unplaced": unplaced[:24],
                          "season_episodes": last, "episode_offset": ep_off,
                          "suggested_offset": guess, "mikan_id": pages[0], "mikan_pages": pages},
            )
            return
        cutoff = (date.fromisoformat(today) - timedelta(days=NO_RELEASE_GRACE_DAYS)).isoformat()
        overdue = [ep for ep in silent if (air_of.get(ep) or today) <= cutoff]
        if not overdue:
            return
        late = [ep for ep in overdue if stalled(ep)]
        yield Finding(
            rule=self.id, kind="episode_not_released", severity="important" if late else "minor",
            subject=f"S{season:02d}",
            summary=(f"「{show.official_title}」S{season} 的 {overdue} 播出已超过 {NO_RELEASE_GRACE_DAYS} 天，"
                     f"番组页（查过 {len(pages)} 个：{', '.join(pages)}）上还没有这几集的任何发布"
                     f"（字幕组断更、番组页选错了，或发布标题认不出集号）"),
            show=show.dir_name,
            evidence={"season": season, "episodes": overdue, "stalled": late,
                      "waiting": [ep for ep in silent if ep not in overdue],
                      "air_dates": {str(ep): air_of.get(ep, "") for ep in overdue},
                      "grace_days": NO_RELEASE_GRACE_DAYS, "mikan_id": pages[0], "mikan_pages": pages},
        )


GRAB_DETECTORS = [EpisodeAvailableDetector]
