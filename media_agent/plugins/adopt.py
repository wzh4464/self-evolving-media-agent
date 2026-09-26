"""把 AutoBangumi 订阅里 media-agent 要用的东西迁进各番的 sidecar：AB 退役之前，它今天提供的每一样先由 media-agent 接住。

用户今天通过 AB 订阅（35 条有效订阅；`autobangumi-subscribe-verify` 流程），AB 以后仍是订阅的前端——但 AB 库里有些
东西只有它知道，AB 一停（或订阅在 AB 里被停用成 `deleted=1`，`AutoBangumiDB.bangumi` 只读 `deleted=0`）就无声地没了：

- **集号偏移**（`episode_offset`，AB 37《超超超超超喜欢你的100个女朋友》第三季的 -24）：迁进 sidecar 的
  `episode_offsets`（`adopt_episode_offset` 动作）。迁之前规则退回 AB 行（`builtin.episode_offset_for`），行为不变。

检测只读（AGENTS.md 第 13 条）：读 AB 库（只读连接）与 sidecar，要写的变成动作，经执行器写、有审计、能回退。AB 库一个字节
都不改。
"""
from __future__ import annotations

from typing import Iterable

from .. import abrow
from .. import sidecar as sc_mod
from ..kernel import Action, Context, Finding, LibraryState
from ..naming import season_of_dir
from ..scan import SKIP_DIRS


def safe_dir_name(name: str) -> bool:
    """订阅行给出的番目录名能不能当成媒体根下的一个目录：单个路径分量、不是隐藏目录 / 扫描跳过的目录 / 季目录。"""
    return bool(name) and name.strip() == name and "/" not in name and "\0" not in name \
        and name not in (".", "..") and not name.startswith(".") and name not in SKIP_DIRS \
        and season_of_dir(name) is None


class AbAdoptionDetector:
    """AB 订阅行上、sidecar 里还没有的东西（见模块文档）。"""
    id = "ab-adoption"
    kind = "ab_episode_offset"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        media_root = ctx.config.media_root
        for row in state.bangumi_rows:
            off = abrow.episode_offset(row)
            if not off:
                continue
            name = abrow.show_dir_name(row, media_root)
            if not safe_dir_name(name) or not (media_root / name).is_dir():
                continue                  # 目录还没有：偏移跟着订阅接手一起写（没有目录就没有可换算的文件）
            show_dir = media_root / name
            sc, problem = sc_mod.load_checked(show_dir)
            if problem:
                continue                  # 坏档案由 sidecar-sync 报（`sidecar_corrupt`），修好之前不迁
            season = abrow.library_season(row)
            if str(season) in (sc.episode_offsets or {}):
                continue                  # 已经登记（人写的、上一轮迁的）：一律以 sidecar 为准
            yield Finding(
                rule=self.id, kind=self.kind, severity="important", subject=f"S{season:02d}",
                summary=(f"AutoBangumi 订阅 {row.get('id')}（第 {season} 季）的集号偏移 {off:+d} 还只记在 AB 库里："
                         f"迁进 sidecar 的 episode_offsets，AB 退役之后判重、改名、抓取照样换算"),
                show=name,
                evidence={"bangumi_id": row.get("id"), "season": season, "episode_offset": off,
                          "ab_season": row.get("season"), "season_offset": row.get("season_offset"),
                          "save_path": row.get("save_path"), "official_title": row.get("official_title")},
                action=Action(op="adopt_episode_offset",
                              args={"show_dir": str(show_dir), "season": season, "offset": off,
                                    "bangumi_id": row.get("id")},
                              note="只在 sidecar 这一季还没有登记时写；已有的一律不改"),
            )


ADOPTION_DETECTORS = [AbAdoptionDetector]
