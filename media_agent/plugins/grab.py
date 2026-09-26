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
from ..cache import Cache, FEED_TTL, LOOKUP_TTL, season_episodes
from ..kernel import (Action, Context, Finding, LibraryState, episode_of_file,
                      tmdb_groups)
from ..naming import (SPECIAL_RE, declared_seasons, offset_episode, parse_episode, parse_pin,
                      season_of_dir, slot_in_season)
from ..sidecar import load as load_sidecar
from .subscription import (MIKAN, _disk_episodes, _http_get,
                           _mikan_search_ids, is_seasonal)

# 单轮为一部番最多提议抓几集，防止新订阅时一次刷屏
MAX_PER_SHOW = 6

# 一集播出多少天之后，番组页上还一个发布都没有才报（`episode_not_released`）。字幕组一般一两天内出；
# 在这之前是正常的等待，每轮都报只会把真问题淹掉。
NO_RELEASE_GRACE_DAYS = 3

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


def _inflight(ctx: Context, show, by_hash: dict) -> dict[int, set[int]]:
    """已经有种子在下、只是还没下完的集：`{季号: {集号…}}`。

    `have` 只认下完的——这是对的，没下完就还有换源的余地。但**已经在下的
    不该无条件重复抓**：新种子会写向同一个目标路径，两个种子抢同一个文件，
    谁也校验不过（见 `colliding-torrent`，本库已发生三次）。

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

        if t and droppable_dead(ctx, t, now):
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


def _pick_key(show, season: int | None) -> str:
    """选中的番组页在缓存里的键：按 TMDB id（目录改名不丢）+ 季——同一部番的不同季常在不同的番组页。"""
    return f"mikanpick:{show.tmdb_id or show.dir_name}:{'' if season is None else season}"


def _resolve_mikan_id(sc, show, cache, air: list[str] | None = None, log=None,
                      season: int | None = None, preferred: str = "") -> str | None:
    """找这部番（这一季）在 Mikan 上的番组 id。

    候选的来源是有讲究的：
    0. 这一季的订阅里记着的番组页（`preferred`：AB 订阅的 bangumiId、`subscribe --mikan`）——人为这一季选的，最先试
    1. 上一轮为这一季选中的（`state/` 缓存里的 `mikanpick:`）与 sidecar 里的 `mikan_id` —— 先试
    2. 从已有的 rss_link 里抠 —— 番组式链接里就带着 bangumiId，
       这是**已被验证过的**映射（订阅确实从它拿到过内容）
    3. 才轮到按标题搜 —— 最不可靠：Mikan 的番组命名常年错位
       （《入间同学入魔了！》标"第三季"的页面装的是第一季），
       按标题搜到的 id 只能算猜测

    **选中的记进缓存，不写 sidecar。** 以前在这里 `save_sidecar`——那是检测期：`diagnose`、
    `apply --dry-run`、演进器的影子验证都会改写媒体库里的 `.media-agent.json`，改动没有审计、回退不了
    （2026-09-26 状态测绘：生产上 5 份 sidecar 在 12:01–12:02 被改写，最后一条审计停在 11:17）。
    检测只许读媒体根；sidecar 里的 `mikan_id` 从此只由人写，这里只读它当候选。
    """
    stored = str(getattr(sc, "mikan_id", "") or "")
    key = _pick_key(show, season)
    remembered = str((cache.get_llm(key, ttl=PICK_TTL) or {}).get("id") or "")

    preferred = str(preferred or "")

    # 没有播出日期可比时，只能沿用存下来的（老番、TMDB 查不到的情况）
    if (preferred or remembered or stored) and not air:
        return preferred or remembered or stored

    cands: list[str] = []
    for c in (preferred, remembered, stored):
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
    if not cands:
        return None

    # 用播出日期给每个候选页面打分，取最高的。
    # 这一步是必要的：入间同学的 rss_link 是搜索式链接（没有 bangumiId），
    # 于是只能按标题搜，搜到的 2839 是**第三季**的页面——它每一集都齐、
    # 每个集号都对得上，唯独年份差了三年。不比时间就发现不了。
    best, best_fit = None, 0.0
    for mid in cands[:4]:
        try:
            fit = _season_fit(_feed_cached(mid, cache), air)
        except Exception as e:
            if log:
                log(f"[episode-available] 拉候选番组页 {mid} 的 feed 失败：{type(e).__name__}: {e}")
            continue
        if fit > best_fit:
            best, best_fit = mid, fit
    if best is None or best_fit < 0.15:
        # 一个都对不上：宁可不抓，也不要从错的季里抓。
        # 返回 None 会让上层报"找不到 Mikan 番组页"，那是准确的描述。
        return None

    if best != remembered:
        cache.put_llm(key, {"id": str(best), "fit": round(best_fit, 3)})
    return str(best)


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

        for show in state.shows:
            if not show.tmdb_id or show.naming_hold:
                continue                 # 标题认不准：抓来的会按退回的名字改名、落进那个分类
            g = groups.get(show.tmdb_id) or {}
            if g.get("kind") == "duplicate" and show in g.get("duplicates", []):
                host = g["host"]
                if show.tmdb_id not in dup_reported:
                    dup_reported.add(show.tmdb_id)
                    sibs = [s.dir_name for s in g["shows"]]
                    yield Finding(
                        rule=self.id, kind="duplicate_show_dir", severity="important",
                        summary=(f"{len(sibs)} 个目录装着同一批内容（TMDB "
                                 f"{show.tmdb_id}），抓取只认文件最多的"
                                 f"「{host.dir_name}」，其余会被跳过"),
                        show=host.dir_name,
                        evidence={"tmdb_id": show.tmdb_id, "dirs": sibs,
                                  "host": host.dir_name,
                                  "duplicates": [d.dir_name for d in g["duplicates"]],
                                  "hint": "多半是历史遗留的分季目录或繁简两版，应合并"},
                    )
                continue
            sc = load_sidecar(show.dir_path)
            # 要看的季：盘上有文件的（sidecar-sync 记在 `seasons` 里）+ 订阅了的（`subscriptions`：盘上一集都还没有
            # 也抓——新番、新一季以前要等 AutoBangumi 放进第一个文件）
            subs = {str(k): (v if isinstance(v, dict) else {})
                    for k, v in (sc.subscriptions or {}).items() if str(k).isdigit()}
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
            # 换算需要 sidecar 的 `season_offsets`（发布方季号 → 之前累计集数），
            # 那是人或演进器该决定的事。在配好之前宁可不抓。
            tmdb_sn = {int(x["season_number"]) for x in (show.tmdb_seasons or [])
                       if x.get("season_number")}
            lib_sn = {int(k) for k, v in sc.seasons.items()
                      if k.isdigit() and int(k) > 0 and v.get("have")}
            extra = sorted(lib_sn - tmdb_sn) if tmdb_sn else []
            if extra:
                yield Finding(
                    rule=self.id, kind="season_layout_mismatch", severity="important",
                    classified=True,
                    summary=(f"库内有 Season {extra} 而 TMDB 只有 "
                             f"Season {sorted(tmdb_sn)}——两套集号口径不一致，"
                             f"已停止对这部番自动抓取，以免把已有的集数重下一遍。"
                             f"按 TMDB 重编排目录，或在 sidecar 的 season_offsets "
                             f"里登记换算关系"),
                    show=show.dir_name,
                    evidence={"library_seasons": sorted(lib_sn),
                              "tmdb_seasons": sorted(tmdb_sn), "extra": extra},
                )
                continue
            inflight = _inflight(ctx, show, by_hash)

            disk_eps = _disk_episodes(show)

            for season_key in season_keys:
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
                eps, why = season_episodes(ctx, cache, show.tmdb_id, int(season_key))
                if eps is None:
                    ctx.log(f"[episode-available] TMDB 第 {season_key} 季集表读取失败 {show.dir_name}，"
                            f"这一季这一轮不抓：{why}")
                    continue
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
                if not is_seasonal(dates, len(eps), date.today()):
                    continue
                aired = {n for n, d in air_of.items() if d <= today}
                # 已经在下的不重复抓（除非停滞太久，见 _inflight 的注释）
                busy = inflight.get(int(season_key), set())
                missing = sorted(aired - have - busy)
                if not missing:
                    continue

                mid = _resolve_mikan_id(
                    sc, show, cache, log=ctx.log, season=int(season_key),
                    air=[d for d in (air_of.get(n) for n in aired) if d],
                    preferred=str((subs.get(season_key) or {}).get("mikan_id") or ""))
                if not mid:
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="minor",
                        subject=f"S{int(season_key):02d}",
                        summary=(f"「{show.official_title}」缺 {len(missing)} 集"
                                 f"{missing[:8]}，但找不到 Mikan 番组页，无从抓取"),
                        show=show.dir_name,
                        evidence={"missing": missing, "season": season_key},
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
                ep_off = episode_offset_for(show, int(season_key)) if int(season_key) else 0
                by_ep: dict[int, list[dict]] = {}
                off_season: dict[int, list[tuple[dict, str]]] = {}
                for it in items:
                    n = _episode_of(it["title"])
                    if n is None:
                        continue
                    ep_here, why = _slot_in_season(it["title"], n, int(season_key), off_by_season)
                    if ep_here is not None and ep_off:
                        shifted = offset_episode(ep_here, ep_off)
                        if shifted is None:
                            why = (f"按集号偏移 {ep_off:+d} 换算出非正数（{ep_here} → {ep_here + ep_off}），"
                                   f"说不清是哪一集")
                        ep_here = shifted
                    if ep_here is None:
                        off_season.setdefault(n, []).append((it, why))
                    else:
                        by_ep.setdefault(ep_here, []).append(it)

                silent: list[int] = []           # 归拢表里什么都没有的集：循环之后统一说
                for ep in missing[:MAX_PER_SHOW]:
                    raw = by_ep.get(ep) or []
                    elsewhere = off_season.get(ep) or []
                    if not raw and not elsewhere:
                        silent.append(ep)
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
                        if wrong_season or elsewhere:
                            yield Finding(
                                rule=self.id, kind=self.kind, severity="minor",
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
                        yield Finding(
                            rule=self.id, kind=self.kind, severity="minor",
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
                    verdict = next(v for c, v in scored if c is best)
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        subject=f"S{int(season_key):02d}E{ep:02d}",
                        summary=(f"「{show.official_title}」S{season_key}E{ep:02d} "
                                 f"可抓取（{len(cands)} 个候选中选 {verdict.why()}）"),
                        show=show.dir_name,
                        evidence={"season": season_key, "episode": ep,
                                  "air_date": air_of.get(ep, ""), "mikan_id": mid,
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
                    yield from self._silent(show, int(season_key), silent, by_ep, eps, air_of, mid,
                                            ep_off, today)

    def _silent(self, show, season: int, silent: list[int], by_ep: dict, eps: list[dict],
                air_of: dict, mid: str, ep_off: int, today: str) -> Iterable[Finding]:
        """要找的集在归拢表里什么都没有（N13 以前的 bare `continue`）：说出来。

        - 番组页上有编号落在这一季**之外**（比 TMDB 这一季最后一集还大）：多半是按连续集号发布、又没登记集号偏移——
          这一季一集都抓不到，报 `episode_numbering_mismatch`（important），带上按最小编号推测的偏移供人核对；
        - 否则是真的还没人发：播出超过 `NO_RELEASE_GRACE_DAYS` 天的才报 `episode_not_released`（minor），刚播的是
          正常的等待。"""
        last = max((int(e["episode_number"]) for e in eps if e.get("episode_number")), default=0)
        unplaced = sorted(n for n in by_ep if n > last)
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
                          "suggested_offset": guess, "mikan_id": mid},
            )
            return
        cutoff = (date.fromisoformat(today) - timedelta(days=NO_RELEASE_GRACE_DAYS)).isoformat()
        overdue = [ep for ep in silent if (air_of.get(ep) or today) <= cutoff]
        if not overdue:
            return
        yield Finding(
            rule=self.id, kind="episode_not_released", severity="minor", subject=f"S{season:02d}",
            summary=(f"「{show.official_title}」S{season} 的 {overdue} 播出已超过 {NO_RELEASE_GRACE_DAYS} 天，"
                     f"番组页 {mid} 上还没有这几集的任何发布（字幕组断更、番组页选错了，或发布标题认不出集号）"),
            show=show.dir_name,
            evidence={"season": season, "episodes": overdue,
                      "waiting": [ep for ep in silent if ep not in overdue],
                      "air_dates": {str(ep): air_of.get(ep, "") for ep in overdue},
                      "grace_days": NO_RELEASE_GRACE_DAYS, "mikan_id": mid},
        )


GRAB_DETECTORS = [EpisodeAvailableDetector]
