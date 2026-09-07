"""扫描：把磁盘 / qBittorrent / AutoBangumi 三方状态汇成一份 LibraryState。

原则：**磁盘是文件名的唯一事实来源**。qBittorrent 的 `name` 字段在
`renameFile` 之后不会更新，用它判断改名状态会大量误报（实测踩过）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .kernel import Context, LibraryState, MediaFile, Show, under
from .naming import SUB_EXTS, VIDEO_EXTS, season_of_dir

SKIP_DIRS = {".autobangumi", "@eaDir", ".Trash", "lost+found"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}


def _iter_files(show_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in show_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES or p.name.startswith("._"):
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        suffix = p.suffix.lower()
        stem_suffix = Path(p.stem).suffix.lower() if p.suffix == ".!qB" else ""
        if suffix in VIDEO_EXTS or suffix in SUB_EXTS or suffix == ".!qB" or stem_suffix in VIDEO_EXTS:
            out.append(p)
    return out


def _torrent_files(ctx: Context, torrent_hash: str) -> list[dict]:
    """取种子的文件列表。单次扫描内按 hash 缓存，避免同一种子重复请求。"""
    cache = getattr(ctx, "_tfile_cache", None)
    if cache is None:
        cache = {}
        ctx._tfile_cache = cache          # type: ignore[attr-defined]
    if torrent_hash not in cache:
        try:
            cache[torrent_hash] = ctx.qbit.files(torrent_hash)
        except Exception:
            cache[torrent_hash] = []
    return cache[torrent_hash]


def _file_into(show: Show, f: MediaFile) -> None:
    """把文件分流进 `files`（参与整理）或 `extras_files`（已归置）。"""
    (show.extras_files if f.quarantined else show.files).append(f)


def _looks_like_movie(show_dir: Path, video_count: int) -> bool:
    """这个目录装的是一部电影，而不是一部剧集？

    库里剧集和电影混在同一个 Media 根下（178 个目录里 45 个是电影）。
    对电影问"集号是多少"永远没有答案，于是每部电影都会稳定产出一条
    "无法解析集号，需人工或模型判断"——45 条纯噪音，还把真正需要人看的
    条目淹没了。

    判据用结构而不是文件名：**有 `Season N` 子目录的一定是剧集**，
    这是本库雷打不动的组织方式。没有 Season 子目录、视频又不超过 2 个
    （正片 + 可能的特典/预告），按电影处理。

    故意不靠目录名里的 `(YYYY)`：45 个里有 4 个不带年份
    （超时空辉夜姬 / 铃芽之旅 / 小林家的龙女仆剧场版 / 世界奇妙物语SP），
    靠年份会漏掉它们。
    """
    for x in show_dir.iterdir():
        if x.is_dir() and season_of_dir(x.name) is not None:
            return False
    return video_count <= 2


def _season_dir_of(path: Path, show_dir: Path) -> str:
    try:
        rel = path.relative_to(show_dir)
    except ValueError:
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


def build_state(ctx: Context, resolve_tmdb: bool = True) -> LibraryState:
    cfg = ctx.config
    state = LibraryState()

    # --- qBittorrent：按 content_path 建索引 ---
    torrents = ctx.qbit.torrents() if ctx.qbit else []
    state.torrents = torrents
    torrent_by_hash = {t["hash"]: t for t in torrents}
    by_path: dict[str, dict] = {}
    for t in torrents:
        cp = t.get("content_path", "")
        if cp:
            by_path[cp] = t
            # 合集种子：content_path 是目录，成员文件挂在它下面
            by_path.setdefault(cp.rstrip("/"), t)

    # --- AutoBangumi ---
    if ctx.abdb:
        try:
            state.bangumi_rows = ctx.abdb.bangumi()
            state.rss_rows = ctx.abdb.rss_items()
        except Exception as e:
            ctx.log(f"[scan] 读取 AutoBangumi DB 失败: {e}")

    bangumi_by_dir: dict[str, dict] = {}
    for row in state.bangumi_rows:
        sp = row.get("save_path") or ""
        if sp:
            # save_path 形如 .../Media/<show>/Season N
            parts = Path(sp).parts
            try:
                idx = parts.index(cfg.media_root.name)
                if idx + 1 < len(parts):
                    bangumi_by_dir.setdefault(parts[idx + 1], row)
            except ValueError:
                pass

    # --- 磁盘 ---
    media_root = cfg.media_root
    if not media_root.exists():
        ctx.log(f"[scan] 媒体根目录不存在: {media_root}")
        return state

    for show_dir in sorted(media_root.iterdir()):
        if not show_dir.is_dir() or show_dir.name in SKIP_DIRS or show_dir.name.startswith("."):
            continue

        show = Show(dir_name=show_dir.name, dir_path=show_dir,
                    bangumi=bangumi_by_dir.get(show_dir.name))

        # --- 来源 1：qBittorrent 的文件列表（种子内容的权威来源）---
        #
        # 对有种子的内容，`torrents/files` 才是事实来源，不是磁盘：
        # - renameFile 之后它**会**更新（不更新的是 `torrents/info` 的 `name` 字段，
        #   那是种子显示名，两者不是一回事——早先混淆过这一点）
        # - 它包含尚未落盘的文件（0% 进度时磁盘上什么都没有）
        # - 下载中的文件磁盘上带 `.!qB` 后缀，这里给的是干净的目标名
        # **一个路径只能产出一条 MediaFile。** 多个种子宣称同一路径是常态：
        # 换版本时旧种子被停用、文件被移走，但种子还留在 qBittorrent 里，
        # 它的 `torrents/files` 仍然报着那个路径；新种子 renameFile 到同一个
        # 集位文件名后，两者就重合了。
        #
        # 2026-09-06 尼古喵喵 S01E08 就是这么丢的：扫描发出两条同路径条目，
        # `duplicate-episode` 判定"这一集有 2 个文件"，排序后把"输的那个"
        # 移进隔离区——而两条指的是同一个磁盘文件，于是唯一的真文件没了，
        # 审计里留下一行自相矛盾的 `保留 X，清理 X`。
        #
        # 谁是真正的拥有者：磁盘大小和种子声明大小对得上的那个。对不上就退回
        # 进度高的、再退回已存在于磁盘的。判不出来也没关系——重点是只留一条。
        covered: set[Path] = set()
        claimed: dict[Path, tuple] = {}
        for h, t in torrent_by_hash.items():
            sp = (t.get("save_path") or "").rstrip("/")
            if not sp or not under(sp, show_dir):
                continue
            for entry in _torrent_files(ctx, h):
                if entry.get("priority", 1) == 0:
                    continue            # 被标记为不下载
                abs_p = Path(sp) / entry["name"]
                suffix = abs_p.suffix.lower()
                if suffix not in VIDEO_EXTS and suffix not in SUB_EXTS:
                    continue
                covered.add(abs_p)
                covered.add(Path(str(abs_p) + ".!qB"))
                try:
                    on_disk = abs_p.stat().st_size
                except OSError:
                    on_disk = None
                declared = entry.get("size", 0)
                score = (
                    on_disk is not None and declared == on_disk,   # 大小对得上
                    t.get("progress", 0.0),                        # 进度更高
                    on_disk is not None,                           # 文件真的在
                )
                prev = claimed.get(abs_p)
                if prev is None or score > prev[0]:
                    claimed[abs_p] = (score, h, t, declared)

        for abs_p, (_score, h, t, declared) in claimed.items():
            _file_into(show, MediaFile(
                path=abs_p,
                size=declared,
                show_dir=show_dir.name,
                season_dir=_season_dir_of(abs_p, show_dir),
                filename=abs_p.name,
                torrent_hash=h,
                torrent_name=t.get("name", ""),
                torrent_state=t.get("state", ""),
                torrent_progress=t.get("progress", 0.0),
                torrent_tags=t.get("tags", ""),
                torrent_category=t.get("category", ""),
            ))

        # --- 来源 2：磁盘上没有种子覆盖的文件（纯本地内容）---
        for p in _iter_files(show_dir):
            if p in covered:
                continue
            t = by_path.get(str(p))
            if t is None:
                for parent in p.parents:          # 合集种子：向上找父目录
                    if parent == media_root:
                        break
                    t = by_path.get(str(parent))
                    if t is not None:
                        break

            # 向上找父目录认领种子，对**多文件种子**是危险的：它的 `content_path`
            # 就是季目录本身，于是该目录下每一个孤儿文件都会被认领给它。
            #
            # 来源 1 已经把 save_path 在本剧目录下的种子的文件全部登记进 `covered`，
            # 能走到这里就说明这个文件**不在**那些种子的文件列表里——那它就不属于
            # 它们，认领是错的。实测代价有二：
            #   1. 带着错误 hash 去改名，执行器正确地拒绝（"种子文件列表里找不到
            #      该文件"），《住在拔作岛上的我应该如何是好？》为此失败了 30 次；
            #   2. 更糟的是下面那句 `save_path == p.parent` 的"已覆盖"短路，
            #      把这些文件整个丢弃——8 个正片 + 6 个字幕对所有规则**不可见**，
            #      既不报错也不处理，静静躺在库里。
            if t is not None:
                sp2 = (t.get("save_path") or "").rstrip("/")
                if sp2 and under(sp2, show_dir):
                    t = None                      # 不属于它，按纯本地文件处理
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            _file_into(show, MediaFile(
                path=p,
                size=size,
                show_dir=show_dir.name,
                season_dir=_season_dir_of(p, show_dir),
                filename=p.name,
                torrent_hash=(t or {}).get("hash", ""),
                torrent_name=(t or {}).get("name", ""),
                torrent_state=(t or {}).get("state", ""),
                torrent_progress=(t or {}).get("progress", 0.0),
                torrent_tags=(t or {}).get("tags", ""),
                torrent_category=(t or {}).get("category", ""),
            ))

        if show.files or show.extras_files:
            show.is_movie = _looks_like_movie(
                show_dir, sum(1 for f in show.files if f.ext in VIDEO_EXTS))
            state.shows.append(show)

    # --- 不在 Media 下的种子（下载中/别处）---
    root_str = str(media_root)
    state.orphan_torrents = [
        t for t in torrents if not (t.get("content_path", "") or "").startswith(root_str)
    ]

    if resolve_tmdb and ctx.tmdb and ctx.tmdb.enabled:
        _resolve_tmdb(ctx, state)

    return state


def _resolve_tmdb(ctx: Context, state: LibraryState) -> None:
    """给每部番挂上 TMDB 元数据。带磁盘缓存，避免每轮都打 API。"""
    from .cache import Cache

    cache = Cache(ctx.config.cache_db)
    for show in state.shows:
        # 查询词优先用 AutoBangumi 的 official_title / title_raw，比目录名更干净
        queries = [show.dir_name]
        if show.bangumi:
            for k in ("official_title", "title_raw"):
                v = (show.bangumi.get(k) or "").strip()
                if v and v not in queries:
                    queries.append(v)

        cached = cache.get_tmdb(show.dir_name)
        if cached:
            show.tmdb_id = cached.get("id")
            show.tmdb_title = cached.get("title", "")
            show.tmdb_seasons = cached.get("seasons", [])
            continue

        hit = None
        for q in queries:
            try:
                results = ctx.tmdb.search_tv(q)
            except Exception as e:
                ctx.log(f"[scan] TMDB 查询失败 {q}: {e}")
                continue
            if results:
                hit = _pick_tmdb(ctx, show, q, results)
                if hit:
                    break

        if not hit:
            continue
        try:
            title, _ = ctx.tmdb.official_title(hit["id"])
            seasons = ctx.tmdb.seasons(hit["id"])
        except Exception:
            continue
        show.tmdb_id = hit["id"]
        show.tmdb_title = title
        show.tmdb_seasons = seasons
        cache.put_tmdb(show.dir_name, {"id": hit["id"], "title": title, "seasons": seasons})


def _pick_tmdb(ctx: Context, show: Show, query: str, results: list[dict]) -> dict | None:
    """多个候选时选哪个 —— 确定性规则先行，仍模糊才问模型。"""
    if len(results) == 1:
        return results[0]

    from .naming import normalize
    q = normalize(query)
    exact = [r for r in results
             if normalize(r.get("name", "")) == q or normalize(r.get("original_name", "")) == q]
    if len(exact) == 1:
        return exact[0]

    if ctx.llm and ctx.llm.enabled:
        sample = [f.filename for f in show.files[:5]]
        cands = [{"id": r["id"], "name": r.get("name"),
                  "original_name": r.get("original_name"),
                  "first_air_date": r.get("first_air_date"),
                  "overview": (r.get("overview") or "")[:120]}
                 for r in results[:8]]
        ans = ctx.llm.ask_json(
            system=("你是番剧元数据匹配助手。根据目录名和实际文件名，从 TMDB 候选中选出唯一正确的作品。"
                    "只返回 JSON：{\"id\": <tmdb_id 或 null>, \"confidence\": 0-1, \"reason\": \"...\"}。"
                    "不确定时返回 id=null，不要猜。"),
            user=json.dumps({"目录名": show.dir_name, "查询词": query,
                             "实际文件名样本": sample, "候选": cands},
                            ensure_ascii=False),
        )
        if ans and ans.get("id") and float(ans.get("confidence", 0)) >= 0.7:
            for r in results:
                if r["id"] == ans["id"]:
                    return r
        return None

    return results[0] if exact else None
