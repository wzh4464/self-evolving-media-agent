"""`media-agent subscribe`：不经 AutoBangumi 订阅一部番的一季。

AB 以后仍是订阅的前端（`ab-adoption` 接手它的每条订阅），可 AB 不在、或不想经 AB 时也要能订。命令只做人会做的那几件事：
按 TMDB 条目找（或建）番目录，把人的意图写进 sidecar——订阅（`subscriptions`）、TMDB 身份（`tmdb_source: human`）、番组页
（`mikan_id`）、版本要求（`require_any`）、集号偏移（`episode_offsets`）。**经同一套动作与审计**：新目录走 `create_show_dir`，
已有目录走 `subscribe_season`，都有逆操作、能 `rollback`。然后说出下一次抓取会做什么（`preview`：只读，抓取检测器照原样跑一遍）。

**不覆盖人写的东西**：目录的 sidecar 里已经有不同的 `tmdb_id` / `require_any` / 这一季的 `episode_offsets`，命令拒绝、什么都
不写（退出码 2）——要改就直接编辑 sidecar。这部番已经有目录（某个目录的 sidecar 记着这个 tmdb_id）就订进那个目录，不另建一个：
两个目录抓同一部番，抓取只认文件多的那个（`tmdb_groups`）。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import sidecar as sc_mod
from .claims import fold
from .kernel import Action, Finding


class SubscribeError(Exception):
    """参数不对、TMDB 查不到、和已有的人写的东西冲突：什么都不写，退出码 2。"""


@dataclass
class Plan:
    show_dir: Path
    season: int
    title: str
    create: bool
    finding: Finding | None                    # None = 这一季已经订阅，没什么可写
    notes: list[str] = field(default_factory=list)


def _tmdb_of(show_dir: Path) -> str:
    data, _problem = sc_mod.read_raw(show_dir)
    return str((data or {}).get("tmdb_id") or "")


def _dir_name_of(title: str) -> str:
    """TMDB 标题当目录名：斜杠换成全角（目录名是单个路径分量），去掉首尾空白。"""
    return title.replace("/", "／").strip()


def _where(media_root: Path, tmdb_id: int, title: str, dir_name: str) -> tuple[Path, bool]:
    """订进哪个目录、要不要新建。已有目录按 sidecar 的 tmdb_id 认；目录名比较按 `claims.fold`（生产卷大小写不敏感）。"""
    from .plugins.adopt import safe_dir_name
    try:
        entries = [p for p in media_root.iterdir() if p.is_dir() and safe_dir_name(p.name)]
    except OSError as e:
        raise SubscribeError(f"列不出媒体根 {media_root}（{type(e).__name__}: {e}）") from e
    owners = [p for p in entries if _tmdb_of(p) == str(tmdb_id)]
    by_fold = {fold(p.name): p for p in entries}
    if dir_name:
        if not safe_dir_name(dir_name):
            raise SubscribeError(f"--dir「{dir_name}」当不了番目录名（要是单个目录名，不能以 . 开头、不能叫 Season N）")
        target = by_fold.get(fold(dir_name))
        if owners and target not in owners:
            raise SubscribeError(f"TMDB {tmdb_id} 已经在「{owners[0].name}」里（它的 sidecar 记着这个 tmdb_id）——另订进"
                                 f"「{dir_name}」会有两个目录抓同一部番；要换目录名请先改那个目录")
        return (target, False) if target else (media_root / dir_name, True)
    if len(owners) > 1:
        raise SubscribeError(f"TMDB {tmdb_id} 在 {len(owners)} 个目录的 sidecar 里（"
                             + "、".join(p.name for p in owners[:4]) + "）：用 --dir 指定订阅进哪一个")
    if owners:
        return owners[0], False
    name = _dir_name_of(title)
    if not safe_dir_name(name):
        raise SubscribeError(f"TMDB 标题「{title}」当不了目录名：用 --dir 指定")
    target = by_fold.get(fold(name))
    return (target, False) if target else (media_root / name, True)


def _conflicts(sc, show_dir: Path, tmdb_id: int, season: int, words: list[str], offset: int | None) -> None:
    """已有目录的 sidecar 里人写的东西与命令行不同：拒绝（人写的字段命令不覆盖）。"""
    if sc.tmdb_id and str(sc.tmdb_id) != str(tmdb_id):
        raise SubscribeError(f"「{show_dir.name}」的 sidecar 里 tmdb_id 是 {sc.tmdb_id}，不是 {tmdb_id}：身份只由人改——"
                             f"确认是它就先改 sidecar，或用 --dir 订进别的目录")
    if words and sc.require_any and list(sc.require_any) != words:
        raise SubscribeError(f"「{show_dir.name}」的 sidecar 里已有 require_any = {sc.require_any}，与 --require-any "
                             f"{words} 不同：人写的字段命令不覆盖，要改就直接编辑 sidecar")
    have = (sc.episode_offsets or {}).get(str(season))
    if offset is not None and have is not None and str(have) != str(offset):
        raise SubscribeError(f"「{show_dir.name}」的 sidecar 里第 {season} 季已有 episode_offsets {have}，与 --offset "
                             f"{offset} 不同：人写的字段命令不覆盖，要改就直接编辑 sidecar")


def plan(ctx, *, tmdb_id: int, season: int | None = None, mikan: str = "", dir_name: str = "",
         require_any=(), offset: int | None = None, today: date | None = None) -> Plan:
    """按 TMDB 条目定订阅的季与目录，给出要执行的动作（包在一条 `subscribe` 规则的发现里，经执行器执行）。"""
    from .cache import _brief
    from .clients import seasons_of, title_of

    today = today or date.today()
    if not (ctx.tmdb and ctx.tmdb.enabled):
        raise SubscribeError("没配置 TMDB_API_KEY：订阅按 TMDB 条目认这部番")
    try:
        detail = ctx.tmdb.tv_detail(int(tmdb_id))
    except Exception as e:                            # noqa: BLE001 —— 查不到就拒绝，原因照实说（只留状态码，不带 api_key）
        raise SubscribeError(f"TMDB 上查不到条目 {tmdb_id}（{_brief(e)}）") from e
    title = title_of(detail)[0]
    seasons = sorted(int(s["season_number"]) for s in seasons_of(detail) if int(s["season_number"] or 0) > 0)
    notes: list[str] = []
    if season is None:
        if not seasons:
            raise SubscribeError(f"TMDB 条目 {tmdb_id}「{title}」还没有任何一季：用 --season 指定")
        season = seasons[-1]
    elif season < 0:
        raise SubscribeError(f"--season 不能是负数：{season}")
    elif season not in seasons and season != 0:
        notes.append(f"TMDB 上「{title}」还没有第 {season} 季（有 {seasons}）：订阅照样记下，等它登上 TMDB 抓取才有集可比")
    mikan = str(mikan or "").strip()
    if mikan and not mikan.isdigit():
        raise SubscribeError(f"--mikan 是 Mikan 番组页的数字 id（RSS 链接里的 bangumiId），收到 {mikan!r}")
    words = [w.strip() for w in (require_any or []) if w and w.strip()]

    show_dir, create = _where(Path(ctx.config.media_root), int(tmdb_id), title, dir_name)
    sub = {"source": "cli", "since": today.isoformat(), **({"mikan_id": mikan} if mikan else {})}
    intent: dict = {}
    if mikan:
        intent["mikan_id"] = mikan
    if words:
        intent["require_any"] = words
    if offset is not None:
        intent["episode_offsets"] = {str(season): int(offset)}
    evidence = {"tmdb_id": int(tmdb_id), "title": title, "season": season, "tmdb_seasons": seasons}
    if create:
        intent.update(subscriptions={str(season): sub}, tmdb_id=int(tmdb_id), tmdb_source="human")
        action = Action(op="create_show_dir",
                        args={"show_dir": str(show_dir), "seasons": [season], "intent": intent},
                        note="建番目录与 Season N、写一份只有人的意图的 sidecar")
        summary = f"订阅「{title}」第 {season} 季：新建番目录「{show_dir.name}」"
    else:
        sc, problem = sc_mod.load_checked(show_dir)
        if problem:
            raise SubscribeError(f"「{show_dir.name}」的 sidecar 解析不了（{problem}）：修好再订")
        _conflicts(sc, show_dir, int(tmdb_id), season, words, offset)
        if str(season) in (sc.subscriptions or {}):
            notes.append(f"「{show_dir.name}」第 {season} 季已经订阅（{sc.subscriptions[str(season)]}），没有要写的")
            return Plan(show_dir, season, title, False, None, notes)
        if not sc.tmdb_id:
            intent.update(tmdb_id=int(tmdb_id), tmdb_source="human")
        action = Action(op="subscribe_season",
                        args={"show_dir": str(show_dir), "season": season, "subscription": sub,
                              "intent": intent},
                        note="只在这一季还没有订阅时写；已有的人写的字段一律不改")
        summary = f"订阅「{title}」第 {season} 季：登记进已有的番目录「{show_dir.name}」"
    finding = Finding(rule="subscribe", kind="subscribe", severity="important", subject=f"S{season:02d}",
                      summary=summary, show=show_dir.name, evidence=evidence, action=action)
    return Plan(show_dir, season, title, create, finding, notes)


def preview(ctx, show_dir: Path, season: int) -> list[str]:
    """下一次抓取会对这部番的这一季做什么：扫描一遍、抓取检测器照原样跑一遍（只读），逐条说；不在播的季说清楚不会抓。"""
    from .cache import Cache, season_episodes
    from .plugins.grab import EpisodeAvailableDetector
    from .plugins.subscription import SEASONAL_WINDOW_DAYS, cour_start, is_seasonal
    from .scan import build_state

    state = build_state(ctx)
    show = next((s for s in state.shows if fold(s.dir_path) == fold(show_dir)), None)
    if show is None:
        return [f"扫描里还看不到「{show_dir.name}」（目录不在、或 sidecar 读不了）"]
    lines: list[str] = []
    if state.qbit_errors:
        lines.append(f"⚠️  qBittorrent 数据不完整（{state.qbit_errors[0]}）：下面的「已有 / 在下」可能不准")
    if show.naming_hold:
        lines.append(f"这一轮不会抓：{show.naming_hold}")
    if not show.tmdb_id:
        return lines + ["还没认出 TMDB 身份：抓取要等扫描认出它（sidecar 的 tmdb_id）"]
    today = date.today()
    eps, why = season_episodes(ctx, Cache(ctx.config.cache_db), show.tmdb_id, season)
    if eps is None:
        return lines + [f"TMDB 第 {season} 季的分集表这会儿取不到（{why}）：抓取要等它"]
    dates = []
    for e in eps:
        try:
            dates.append(date.fromisoformat(str(e.get("air_date") or "")))
        except ValueError:
            continue
    aired = [d for d in dates if d <= today]
    if not is_seasonal(dates, len(eps), today):
        start = cour_start(dates).isoformat() if dates else "未定档"
        return lines + [f"第 {season} 季不在播（这一档开播于 {start}，超过 {SEASONAL_WINDOW_DAYS} 天，或是上百集的连载）："
                        f"抓取只补在播 / 刚播完的季，下一次抓取不会抓它——老番请手动加种"]
    lines.append(f"第 {season} 季在播：TMDB 上已播 {len(aired)}/{len(eps)} 集")
    related = [s for s in state.shows if s is show or s.tmdb_id == show.tmdb_id]
    try:
        found = [f for f in EpisodeAvailableDetector().detect(ctx, dataclasses.replace(state, shows=related))
                 if f.show == show.dir_name and str(f.subject).startswith(f"S{season:02d}")]
    except Exception as e:                            # noqa: BLE001 —— 预览是给人看的：出错照实说，订阅已经写好了
        return lines + [f"预览抓取时出错（{type(e).__name__}: {e}）；订阅已经记下，下一次抓取照常"]
    for f in found:
        lines.append(("  ⬇️  " if f.action else "  ·  ") + f"{f.subject} {f.summary}")
    if not found:
        lines.append("  没有要抓的：已播的都已经有了（或在下），没播的等播了再抓")
    return lines
