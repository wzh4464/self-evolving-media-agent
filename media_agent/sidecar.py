"""每部番目录下的 `.media-agent.json` —— 采集决策的随身档案。

**为什么需要它。** 这套系统里的"真相"散落在三处，且各自会漂移：

| 位置 | 存了什么 | 漂移方式 |
|---|---|---|
| AutoBangumi DB | title_raw / group_name / save_path | 目录改名后 save_path 需手动同步；bangumi_id 大量为空 |
| qBittorrent | 分类 / 标签 / 文件路径 | 分类会碎片化；`info.name` 与 `files.name` 语义不同 |
| 磁盘 | 目录名 / 文件名 | 被 TMDB 标题对齐整体改过 |

结果是"订阅 ↔ 目录"的反查要连试三种方式（save_path → official_title → 归一化模糊）
才勉强对上。更糟的是，**没有任何一处记录"这部番当前该用哪个字幕组、上次更新到几集"**——
字幕组弃坑只能靠人发现"怎么没有后续了"。

这个 sidecar 不是再镜像一份别处已有的数据，而是记录**别处根本没有的东西**：
media-agent 自己的采集决策、观察到的别名、源的更替历史。它跟着目录走，
目录改名/搬迁都不会丢，AutoBangumi 的库炸了也能据此重建订阅。

文件名以 `.` 开头，Jellyfin/Infuse 不会扫描它。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

SIDECAR_NAME = ".media-agent.json"
SCHEMA_VERSION = 1


@dataclass
class SourceRecord:
    """一次"用哪个源"的决策。"""
    group: str                      # 字幕组
    rss_link: str = ""
    adopted_on: str = ""            # 何时采用
    retired_on: str = ""            # 何时弃用（空 = 仍在用）
    last_episode: int = 0           # 该源出到第几集
    reason: str = ""                # 采用/弃用的原因


@dataclass
class Sidecar:
    schema_version: int = SCHEMA_VERSION
    updated_at: str = ""

    # --- 身份：各处叫什么 ---
    canonical_title: str = ""       # 规范名（= 目录名 = TMDB 本地化标题）
    tmdb_id: int | None = None
    tmdb_title: str = ""
    aliases: list[str] = field(default_factory=list)   # 实际发布标题里见过的名字

    # --- 订阅 ---
    bangumi_id: int | None = None   # AutoBangumi 记录 id（可能失效，故不作唯一依据）
    sources: list[dict] = field(default_factory=list)  # SourceRecord 列表，含历史

    # --- 进度 ---
    seasons: dict[str, dict] = field(default_factory=dict)
    # {"1": {"have": [1,2,3], "aired": 7, "total": 12, "next_air": "2026-08-22"}}

    # --- 观察记录 ---
    notes: list[str] = field(default_factory=list)

    @property
    def current_source(self) -> dict | None:
        for s in self.sources:
            if not s.get("retired_on"):
                return s
        return None

    def adopt_source(self, group: str, rss_link: str, reason: str = "") -> None:
        """换源：把当前源标记为退役，登记新源。保留历史，别覆盖。"""
        today = date.today().isoformat()
        cur = self.current_source
        if cur:
            if cur.get("group") == group and cur.get("rss_link") == rss_link:
                return                       # 没变化
            cur["retired_on"] = today
            if not cur.get("reason"):
                cur["reason"] = "被替换"
        self.sources.append(asdict(SourceRecord(
            group=group, rss_link=rss_link, adopted_on=today, reason=reason)))

    def add_alias(self, alias: str) -> bool:
        alias = (alias or "").strip()
        if alias and alias not in self.aliases:
            self.aliases.append(alias)
            return True
        return False

    def note(self, text: str) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        self.notes.append(f"[{stamp}] {text}")
        del self.notes[:-50]            # 只留最近 50 条


def asdict_of(sc: 'Sidecar') -> dict:
    return asdict(sc)


def path_for(show_dir: Path) -> Path:
    return show_dir / SIDECAR_NAME


def load(show_dir: Path) -> Sidecar:
    p = path_for(show_dir)
    if not p.exists():
        return Sidecar(canonical_title=show_dir.name)
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Sidecar(canonical_title=show_dir.name)
    known = {f for f in Sidecar.__dataclass_fields__}
    return Sidecar(**{k: v for k, v in data.items() if k in known})


def save(show_dir: Path, sc: Sidecar) -> Path:
    sc.updated_at = datetime.now().isoformat(timespec="seconds")
    sc.schema_version = SCHEMA_VERSION
    p = path_for(show_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(sc), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)                      # 原子替换，避免半截文件
    return p
