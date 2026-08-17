"""扫描：把磁盘 / qBittorrent / AutoBangumi 三方状态汇成一份 LibraryState。

原则：**磁盘是文件名的唯一事实来源**。qBittorrent 的 `name` 字段在
`renameFile` 之后不会更新，用它判断改名状态会大量误报（实测踩过）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .kernel import Context, LibraryState, MediaFile, Show
from .naming import SUB_EXTS, VIDEO_EXTS

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
        covered: set[Path] = set()
        for h, t in torrent_by_hash.items():
            sp = (t.get("save_path") or "").rstrip("/")
            if not sp or not sp.startswith(str(show_dir)):
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
                show.files.append(MediaFile(
                    path=abs_p,
                    size=entry.get("size", 0),
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
            if t is not None and Path(t.get("save_path") or "") == p.parent:
                continue                          # 已由来源 1 覆盖
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            show.files.append(MediaFile(
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

        if show.files:
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
