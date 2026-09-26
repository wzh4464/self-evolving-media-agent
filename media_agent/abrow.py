"""AutoBangumi 订阅行（`bangumi` 表的一行）上 media-agent 要读的几样：落在哪个番目录、哪个库内季、哪个番组页。

AB 自己的口径（3.2.6 源码只读核对，2026-09-26）：

- 下载落进 `<downloader.path>/<official_title (year)>/Season <season + season_offset>`
  （`downloader/path.py::_gen_save_path`；加起来不到 1 就退回 `season`）。`save_path` 记的就是这个路径——
  目录被 media-agent 改名之后 `rename_show_dir` 会把它同步改掉，所以它一直指着此刻的番目录。
- 改名时集号 = 原始集号 + `episode_offset`（`manager/renamer.py::gen_path`；换算出非正数时 AB 退回原始集号，
  media-agent 不这么猜，见 `naming.offset_episode`）。
- `rss_link` 是番组页 RSS（`…/RSS/Bangumi?bangumiId=…&subgroupid=…`）或搜索式 RSS（没有 bangumiId）。

订阅行是 AB 的数据：这里只读，不改。media-agent 把它要用的迁进各番的 sidecar（`plugins/adopt.py`），AB 退役之后
照样有。
"""
from __future__ import annotations

import re
from pathlib import Path

from .naming import season_of_dir


def library_season(row: dict) -> int:
    """这条订阅的集落进库里的第几季：`save_path` 的 `Season N` > `season + season_offset`（不到 1 退回 `season`）。"""
    sp = str(row.get("save_path") or "").rstrip("/")
    if sp:
        sn = season_of_dir(Path(sp).name)
        if sn is not None:
            return sn
    try:
        season = int(row.get("season") or 1)
        adjusted = season + int(row.get("season_offset") or 0)
    except (TypeError, ValueError):
        return 1
    return adjusted if adjusted >= 1 else season


def show_dir_name(row: dict, media_root: str | Path) -> str:
    """订阅落在媒体根下的哪个番目录（`save_path` 里媒体根目录名后面那一段）；认不出返回空串。

    与扫描认订阅（`scan.build_state`）同一个口径：按媒体根的**目录名**找，不要求整条路径相同——AB 在容器里
    看到的挂载点可以与宿主不同。"""
    parts = Path(str(row.get("save_path") or "")).parts
    root = Path(media_root).name
    if root not in parts:
        return ""
    i = parts.index(root)
    return parts[i + 1] if i + 1 < len(parts) else ""


_BANGUMI_ID = re.compile(r"[?&]bangumiId=(\d+)")


def mikan_id_of(rss_link: str | None) -> str:
    """番组页 RSS 里的 bangumiId（Mikan 番组页 id）；搜索式 RSS 没有，返回空串。"""
    m = _BANGUMI_ID.search(str(rss_link or ""))
    return m.group(1) if m else ""


def episode_offset(row: dict) -> int:
    """订阅行上的 `episode_offset`；没有 / 写坏了按 0。"""
    try:
        return int(row.get("episode_offset") or 0)
    except (TypeError, ValueError):
        return 0
