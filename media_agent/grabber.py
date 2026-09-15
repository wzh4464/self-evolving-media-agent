"""加种子并在**下载过程中**就把它改成规范名。

**为什么必须在下载中改，而不是下完再改**：从加种子到下载完成这段时间里，
文件在磁盘上叫的是发布名。这段时间内——

- 刮削器扫到它，认不出是哪一集，留下空条目；
- `duplicate-episode` 按集位分桶，发布名解析不出集号就不进桶，
  同一集的另一个版本进来时看不见它，于是重复判定失效；
- AutoBangumi 读的是**种子显示名**而不是文件名，显示名不改，
  它下一轮会按自己的理解再改一次，两边打架。

所以正确时机不是"加种子时"，也不是"下完时"，而是**元数据一到手**：
`torrents/files` 有内容的那一刻就能 `renameFile`，此时文件可能一个字节
都还没下。qBittorrent 会把后续分片直接写进新名字。

**这是加种子的唯一入口。** 2026-09-15 审计发现同一件事散落在三处手写
HTTP（`actions.py` 两处 `torrents/add`、一处 `torrents/rename`），
会话里的临时脚本又抄了四五份，各自的等待时长、409 处理、是否改显示名
都不一样。
"""
from __future__ import annotations

import time
from pathlib import Path

from .naming import VIDEO_EXTS


def wait_metadata(qbit, torrent_hash: str, timeout: float = 30.0,
                  interval: float = 1.0) -> list[dict]:
    """等到 `torrents/files` 有内容为止，返回参与下载的文件列表。

    磁力链刚加进来时处于 `metaDL`——还在从 DHT/peer 拉元数据，此时
    `torrents/files` 是空的，任何 `renameFile` 都会失败。拉不到元数据的
    常见原因是连不上任何 peer（无端口转发时尤其常见），那可能要几分钟，
    所以超时返回空列表是正常结果，不是错误：交给后续的 `unrenamed-file`
    规则兜底即可。
    """
    deadline = time.time() + timeout
    while True:
        try:
            files = [f for f in (qbit.files(torrent_hash) or [])
                     if f.get("priority", 1) != 0]
        except Exception:
            files = []
        if files:
            return files
        if time.time() >= deadline:
            return []
        time.sleep(interval)


def rename_single_video(qbit, torrent_hash: str, target_stem: str,
                        files: list[dict] | None = None) -> str | None:
    """把种子里**唯一**的正片文件改成 `target_stem + 原扩展名`，返回新文件名。

    只处理"恰好一个视频文件"的种子。合集、带特典的多文件种子在这里不猜——
    哪个文件对应哪一集需要逐个判断，那是 `unrenamed-file` 规则的职责。
    返回 None 表示没改（不是单文件、已经是目标名、或元数据没到）。
    """
    if files is None:
        files = [f for f in (qbit.files(torrent_hash) or [])
                 if f.get("priority", 1) != 0]
    vids = [f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXTS]
    if len(vids) != 1:
        return None
    cur = vids[0]["name"]
    want = target_stem + Path(cur).suffix.lower()
    if cur == want:
        return None
    qbit.rename_file(torrent_hash, cur, want)
    return want


def add_and_name(qbit, source: bytes | str, *, torrent_hash: str,
                 target_stem: str, save_path: str, category: str = "",
                 tags: str = "", metadata_timeout: float = 30.0,
                 rename_torrent: bool = True, log=None) -> dict:
    """加种子 → 等元数据 → 改文件名 → 改种子显示名。一条龙，失败不致命。

    `torrent_hash` 必须由调用方给出（.torrent 可以算 infohash，磁力链里带着），
    因为 `torrents/add` 不返回 hash。

    `rename_torrent` 控制是否同时改**显示名**。改它是为了让 AutoBangumi
    对这个种子的解析结果和我们一致——AB 读种子名不读文件名。自己抓的种子
    已经落在独立分类里、AB 看不见，但改了没坏处；由别的渠道加进来的就必须改。

    返回 `{"added", "renamed", "torrent_renamed", "error"}`，不抛异常：
    改名失败不该让"已经下起来了"这件事算失败，下一轮规则会兜底。
    """
    out = {"added": False, "renamed": None, "torrent_renamed": False, "error": ""}
    try:
        out["added"] = qbit.add_torrent(
            source, save_path=save_path, category=category, tags=tags)
    except Exception as e:
        out["error"] = "加种子失败: %s" % e
        return out

    try:
        files = wait_metadata(qbit, torrent_hash, timeout=metadata_timeout)
        if not files:
            out["error"] = ("元数据未在 %.0f 秒内到达（多半是连不上 peer），"
                            "改名交给 unrenamed-file 兜底" % metadata_timeout)
            return out
        out["renamed"] = rename_single_video(qbit, torrent_hash, target_stem, files)
        if rename_torrent:
            qbit.rename_torrent(torrent_hash, target_stem)
            out["torrent_renamed"] = True
    except Exception as e:
        out["error"] = "改名失败（下一轮会补）: %s" % e
        if log:
            log("[grab] %s" % out["error"])
    return out
