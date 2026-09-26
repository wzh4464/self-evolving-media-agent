"""订阅着的番下一季开播时，登记新一季的订阅（`subscribe_season`，sidecar 的 `subscriptions`）。

AutoBangumi 的订阅是一季一行：新一季要人在 AB 里再订一次（`autobangumi-subscribe-verify` 流程专门讲 "Season N" 续作）。
media-agent 的抓取只看盘上有文件的季与订阅的季——AB 的下载一关，老番的新一季就不会来（critic §4）。

**什么算"订阅着"**：sidecar 里有 `subscriptions`（`media-agent subscribe`、AB 订阅接手、上一次登记的新季），或者 AB 里有它
的有效订阅（`deleted=0`，扫描按 save_path 挂到 `show.bangumi`）。档案里留着的旧 `bangumi_id` 不算：那可能是人在 AB 里停用了
的订阅——人不追了，不替他订。要停：在 AB 里停用，并删掉 sidecar 的 `subscriptions`。

**什么算"开播"**：TMDB 上这部番的下一季（库里最大季号 + 1）有定了档的集，第一集在 `NEW_SEASON_LEAD_DAYS` 天之内（或已经
播了），而且是在播的季（`is_seasonal`：不是老早以前播完的、不是上百集的常年连载）。一周的提前量让开播当天的第一集赶得上。

**不登记**：库内编号与 TMDB 对不上的——sidecar 有 `season_offsets`（TMDB 压平成一季、或发布方分季换算进来的番），或者库里
最大那一季的集数比 TMDB 那一季还多（库里按连续编号）。那种番 TMDB 的"下一季"多半就是库里已有的后半段，登记了只会再抓一遍。
要人看就在 sidecar 里自己写订阅。

下一季的分集表走缓存（`cache.season_episodes`：在播 6 小时；TMDB 上还没有这一季的 404 同样 6 小时内不再问），不看扫描缓存的
季列表——那是按条目缓存 30 天的，新一季上了 TMDB 要等它过期才看得见。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from .. import abrow
from .. import sidecar as sc_mod
from ..cache import Cache, season_episodes
from ..kernel import Action, Context, Finding, LibraryState, have_episodes
from .subscription import is_seasonal

# 下一季第一集在多少天之内开播就登记（已经开播的也登记）
NEW_SEASON_LEAD_DAYS = 7


class NewSeasonDetector:
    id = "new-season"
    kind = "new_season"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not (ctx.tmdb and ctx.tmdb.enabled):
            return
        cache = Cache(ctx.config.cache_db)
        today = date.today()
        for show in state.shows:
            if not show.tmdb_id or show.naming_hold or show.is_movie:
                continue
            sc, problem = sc_mod.load_checked(show.dir_path)
            if problem or sc.season_offsets:
                continue                  # 坏档案由 sidecar-sync 报；压平 / 换算进来的番：TMDB 的季与库内的季对不上
            subs = {int(k) for k in (sc.subscriptions or {}) if str(k).isdigit()}
            if not subs and not show.bangumi:
                continue                  # 没人订着：不替人订
            have = have_episodes(show, allow_release_names=False)
            known = ({int(k) for k in (sc.seasons or {}) if str(k).isdigit()} | subs | set(have)
                     | ({abrow.library_season(show.bangumi)} if show.bangumi else set())) - {0}
            if not known:
                continue
            latest = max(known)
            count = next((int(x.get("episode_count") or 0) for x in show.tmdb_seasons or []
                          if x.get("season_number") == latest), 0)
            if count and max(have.get(latest) or {0}) > count:
                continue                  # 库里这一季比 TMDB 那一季还长：库里是连续编号，TMDB 的下一季就在里面
            nxt = latest + 1
            eps, _why = season_episodes(ctx, cache, show.tmdb_id, nxt)
            if not eps:
                continue                  # TMDB 上还没有下一季（404 是常态，缓存 6 小时）；连不上的由抓取那边说
            dates = []
            for e in eps:
                try:
                    dates.append(date.fromisoformat(str(e.get("air_date") or "")))
                except ValueError:
                    continue
            if not dates:
                continue                  # 还没定档
            first = min(dates)
            if first > today + timedelta(days=NEW_SEASON_LEAD_DAYS) or not is_seasonal(dates, len(eps), today):
                continue
            sub = {"source": "new-season", "after": latest, "tmdb_id": show.tmdb_id,
                   "first_air": first.isoformat()}
            yield Finding(
                rule=self.id, kind=self.kind, severity="important", subject=f"S{nxt:02d}",
                summary=(f"「{show.official_title}」第 {nxt} 季 {first.isoformat()} "
                         f"{'开播' if first <= today else '就要开播'}（TMDB），第 {latest} 季是订阅着的："
                         f"登记新一季的订阅，抓取接着抓"),
                show=show.dir_name,
                evidence={"tmdb_id": show.tmdb_id, "season": nxt, "after": latest,
                          "first_air": first.isoformat(), "episodes": len(eps),
                          "subscribed_by": sorted(subs) or f"AB {show.bangumi.get('id')}"},
                action=Action(op="subscribe_season",
                              args={"show_dir": str(show.dir_path), "season": nxt, "subscription": sub,
                                    "intent": {}},
                              note="只在这一季还没有订阅时写；不对就删掉 sidecar 里这一季的订阅"),
            )


NEW_SEASON_DETECTORS = [NewSeasonDetector]
