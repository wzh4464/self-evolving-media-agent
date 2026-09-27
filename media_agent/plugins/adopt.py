"""把 AutoBangumi 订阅里 media-agent 要用的东西迁进各番的 sidecar：AB 退役之前，它今天提供的每一样先由 media-agent 接住。

用户今天通过 AB 订阅（35 条有效订阅；`autobangumi-subscribe-verify` 流程），AB 以后仍是订阅的前端——但 AB 库里有些
东西只有它知道，AB 一停（或订阅在 AB 里被停用成 `deleted=1`，`AutoBangumiDB.bangumi` 只读 `deleted=0`）就无声地没了：

- **订阅本身**：AB 在的时候，是 AB 把第一个文件放进番目录，media-agent 的扫描与抓取才看得见这部番 / 这一季。现在对每条
  有效订阅：番目录还没有 → `create_show_dir`（建目录、订阅季的 `Season N`、一份只有人的意图的 sidecar，这一步唯一的媒体根
  写入）；番目录在、sidecar 的 `seasons` 与 `subscriptions` 都还没有这一季 → `subscribe_season`。订阅记着
  `rss_link` 里的番组页 id（`mikan_id`），抓取先试它。TMDB 身份不在这里写：扫描按目录名 / AB 标题搜，sidecar-sync 填、
  模型选的走 `pin_tmdb`（原有的一套）。
- **集号偏移**（`episode_offset`，AB 37《超超超超超喜欢你的100个女朋友》第三季的 -24）：迁进 sidecar 的
  `episode_offsets`（`adopt_episode_offset`；目录还没有的随 `create_show_dir` 一起写）。迁之前规则退回 AB 行
  （`builtin.episode_offset_for`），行为不变。

sidecar 的 `seasons` 里已经有的季不写订阅：抓取本来就看它（sidecar-sync 按盘上记进 `seasons`），生产上 35 条订阅不因此
多出 35 次写。盘上有了、`seasons` 还没记的照样写：抓取模式里没有 sidecar-sync，等 6 小时的 `run` 记上之前抓取看不见它。

检测只读（AGENTS.md 第 13 条）：读 AB 库（只读连接）、媒体根的目录列表与 sidecar，要写的变成动作，经执行器写、有审计、
能回退。AB 库一个字节都不改。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .. import abrow
from .. import sidecar as sc_mod
from ..claims import fold
from ..kernel import Action, Context, Finding, LibraryState
from ..naming import season_of_dir
from ..scan import SKIP_DIRS


def safe_dir_name(name: str) -> bool:
    """订阅行给出的番目录名能不能当成媒体根下的一个目录：单个路径分量、不是隐藏目录 / 扫描跳过的目录 / 季目录。"""
    return bool(name) and name.strip() == name and "/" not in name and "\0" not in name \
        and name not in (".", "..") and not name.startswith(".") and name not in SKIP_DIRS \
        and season_of_dir(name) is None


def _dirs_by_bangumi_id(shows) -> dict:
    """AB 订阅 id → 档案里记着它的番目录（sidecar 的 `bangumi_id`，sidecar-sync 按 AB 的 save_path 写的）。"""
    out: dict = {}
    for s in shows:
        bid = sc_mod.load(s.dir_path).bangumi_id
        if bid is not None:
            out.setdefault(bid, s.dir_path)
    return out


def subscription_of(row: dict) -> dict:
    """一条 AB 订阅在 sidecar `subscriptions` 里的样子：从哪来、AB 的 id、番组页 id（搜索式 RSS 没有）。"""
    sub = {"source": "autobangumi", "bangumi_id": row.get("id")}
    mid = abrow.mikan_id_of(row.get("rss_link"))
    if mid:
        sub["mikan_id"] = mid
    return sub


class AbAdoptionDetector:
    """AB 订阅行上、sidecar 里还没有的东西（见模块文档）。"""
    id = "ab-adoption"
    kind = "ab_episode_offset"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        media_root = Path(ctx.config.media_root)
        if not state.bangumi_rows:
            return
        try:
            # 生产卷是大小写 / 规范化都不敏感的 APFS：`re zero` 与 `Re Zero` 是同一个目录（`claims.fold`）
            existing = {fold(p.name): p for p in media_root.iterdir() if p.is_dir()}
        except OSError as e:
            ctx.log(f"[ab-adoption] 列不出媒体根 {media_root}（{type(e).__name__}: {e}），这一轮不接手订阅")
            return
        shows = {fold(s.dir_name): s for s in state.shows}
        missing: dict[str, list[dict]] = {}
        for row in state.bangumi_rows:
            name = abrow.show_dir_name(row, media_root)
            if not safe_dir_name(name):
                yield self._unmapped(row, name)
                continue
            d = existing.get(fold(name))
            if d is None:
                missing.setdefault(name, []).append(row)
                continue
            yield from self._existing(d, row)
        known = _dirs_by_bangumi_id(state.shows) if missing else {}
        for name, rows in missing.items():
            elsewhere = [(r, known[r.get("id")]) for r in rows if r.get("id") in known]
            if elsewhere:
                yield from (self._moved(r, name, d) for r, d in elsewhere)
                continue
            yield self._create(media_root / name, rows)

    def _moved(self, row: dict, name: str, show_dir: Path) -> Finding:
        """AB 的 save_path 指向一个不存在的目录，而另一个番目录的 sidecar 记着这条订阅（sidecar-sync 按 save_path 写下的
        `bangumi_id`）：保存路径过期了（目录改名没同步到 AB——`rename_show_dir` 会同步，人手改的不会）。不建新目录：
        建了就是一个只有档案的空壳，抓取把它当重复目录跳过，标题对齐还要把它往已有的目录上改。"""
        return Finding(
            rule=self.id, kind="ab_subscription_moved", severity="important", subject=f"AB{row.get('id')}",
            summary=(f"AutoBangumi 订阅 {row.get('id')} 的 save_path 指向不存在的「{name}」，可「{show_dir.name}」的"
                     f"档案记着这条订阅——AB 的保存路径过期了，不建新目录；在 AB 里把保存路径改成「{show_dir.name}」"),
            show=show_dir.name,
            evidence={"bangumi_id": row.get("id"), "save_path": row.get("save_path"), "dir_name": name,
                      "recorded_in": show_dir.name})

    def _unmapped(self, row: dict, name: str) -> Finding:
        return Finding(
            rule=self.id, kind="ab_subscription_unmapped", severity="important",
            subject=f"AB{row.get('id')}",
            summary=(f"AutoBangumi 订阅 {row.get('id')}「{row.get('official_title')}」的 save_path "
                     f"{row.get('save_path') or '（空）'} 认不出媒体根下的番目录：media-agent 接不住这条订阅"
                     f"（AB 退役之后它就停了）——在 AB 里把保存路径改到媒体根下"),
            show=name or str(row.get("official_title") or ""),
            evidence={"bangumi_id": row.get("id"), "save_path": row.get("save_path"), "dir_name": name})

    def _create(self, show_dir: Path, rows: list[dict]) -> Finding:
        """番目录还没有：一个动作建目录、各订阅季、sidecar（同一个番目录的几条订阅合在一起）。"""
        seasons = sorted({abrow.library_season(r) for r in rows})
        subs: dict[str, dict] = {}
        offsets: dict[str, int] = {}
        for r in sorted(rows, key=lambda r: int(r.get("id") or 0)):
            sn = str(abrow.library_season(r))
            subs.setdefault(sn, subscription_of(r))
            if abrow.episode_offset(r):
                offsets.setdefault(sn, abrow.episode_offset(r))
        intent: dict = {"subscriptions": subs}
        mid = next((s["mikan_id"] for s in subs.values() if s.get("mikan_id")), "")
        if mid:
            intent["mikan_id"] = mid
        if offsets:
            intent["episode_offsets"] = offsets
        ids = [r.get("id") for r in rows]
        return Finding(
            rule=self.id, kind="ab_subscription_new", severity="important",
            summary=(f"AutoBangumi 订阅 {ids}「{rows[0].get('official_title')}」（第 {seasons} 季）的番目录还没有："
                     f"建目录、登记订阅，扫描与抓取从下一次迭代起就看得见它（不用等 AB 放进第一个文件）"),
            show=show_dir.name, path=str(show_dir),
            evidence={"bangumi_ids": ids, "seasons": seasons,
                      "save_paths": [r.get("save_path") for r in rows], "intent": intent},
            action=Action(op="create_show_dir",
                          args={"show_dir": str(show_dir), "seasons": seasons, "intent": intent},
                          note="只建一个新目录与订阅季的 Season N、写一份只有人的意图的 sidecar；目录名被占就不建"),
        )

    def _existing(self, show_dir: Path, row: dict) -> Iterable[Finding]:
        sc, problem = sc_mod.load_checked(show_dir)
        if problem:
            return                        # 坏档案由 sidecar-sync 报（`sidecar_corrupt`），修好之前不写
        season = abrow.library_season(row)
        key = str(season)
        if key not in (sc.seasons or {}) and key not in (sc.subscriptions or {}):
            # 盘上已经有这一季、sidecar-sync 还没记进 `seasons` 的也登记：抓取模式（`grabmode`）里没有 sidecar-sync，以前
            # 等 6 小时的 `run` 记上之前抓取看不见这一季——订阅那一刻 AB 补的第一集名字里就带季号（`S2 - 01`），第二集
            # 晚半天（2026-09-27 审查复现）。多登记一次无害：这一季本来就要抓，订阅还记着 AB 的番组页
            sub = subscription_of(row)
            intent = {"mikan_id": sub["mikan_id"]} if sub.get("mikan_id") and not sc.mikan_id else {}
            yield Finding(
                rule=self.id, kind="ab_subscription", severity="important", subject=f"S{season:02d}",
                summary=(f"AutoBangumi 订阅 {row.get('id')} 要第 {season} 季，盘上还一集都没有：登记进 sidecar 的"
                         f"订阅，抓取不用等 AB 放进第一个文件"),
                show=show_dir.name,
                evidence={"bangumi_id": row.get("id"), "season": season, "subscription": sub,
                          "save_path": row.get("save_path")},
                action=Action(op="subscribe_season",
                              args={"show_dir": str(show_dir), "season": season, "subscription": sub,
                                    "intent": intent},
                              note="只在这一季还没有订阅时写；番组页 id 只在 sidecar 还没有时补"),
            )
        off = abrow.episode_offset(row)
        if off and key not in (sc.episode_offsets or {}):
            yield Finding(
                rule=self.id, kind=self.kind, severity="important", subject=f"S{season:02d}",
                summary=(f"AutoBangumi 订阅 {row.get('id')}（第 {season} 季）的集号偏移 {off:+d} 还只记在 AB 库里："
                         f"迁进 sidecar 的 episode_offsets，AB 退役之后判重、改名、抓取照样换算"),
                show=show_dir.name,
                evidence={"bangumi_id": row.get("id"), "season": season, "episode_offset": off,
                          "ab_season": row.get("season"), "season_offset": row.get("season_offset"),
                          "save_path": row.get("save_path"), "official_title": row.get("official_title")},
                action=Action(op="adopt_episode_offset",
                              args={"show_dir": str(show_dir), "season": season, "offset": off,
                                    "bangumi_id": row.get("id")},
                              note="只在 sidecar 这一季还没有登记时写；已有的一律不改"),
            )


ADOPTION_DETECTORS = [AbAdoptionDetector]
