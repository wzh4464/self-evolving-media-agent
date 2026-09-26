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


def _torrent_files(ctx: Context, torrent_hash: str, errors: list[str]) -> list[dict]:
    """取种子的文件列表。单次扫描内按 hash 缓存，避免同一种子重复请求。

    **读失败必须记下来，不能缓存成空列表。** 以前任何 `files()` 错误都被吞成 `[]`：
    这个种子的文件于是不算"被种子覆盖"，来源 2 又因为 save_path 在本剧目录下
    把认领丢掉，文件就成了无主的纯本地文件——改名走被禁止的文件系统分支、
    隔离跳过种子处理（critic N2）。每轮约 539 次调用，一次 WebUI 超时就够；
    生产上 qBit 超时见过三次（2026-09-19/20）。现在记进 `errors`，
    执行器看到非空就整批拒绝。
    """
    cache = getattr(ctx, "_tfile_cache", None)
    if cache is None:
        cache = {}
        ctx._tfile_cache = cache          # type: ignore[attr-defined]
    if torrent_hash not in cache:
        try:
            cache[torrent_hash] = ctx.qbit.files(torrent_hash)
        except Exception as e:
            msg = f"files({torrent_hash[:8]}) 读取失败：{type(e).__name__}: {e}"
            errors.append(msg)
            ctx.log(f"[scan] qBittorrent 数据不完整——{msg}")
            return []                     # 不缓存：这是"不知道"，不是"没有文件"
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
    # 与 ctx 共用同一个列表：执行器从 ctx 上看本轮扫描是否完整（见 Executor.apply）
    ctx.qbit_errors = state.qbit_errors

    # 每次扫描都是一份新快照：清掉上一次扫描留下的缓存（testinfra B3）。
    # `_tfile_cache` 以前在 Context 的整个生命周期里从不失效，`cmd_run` 在 apply
    # 之后用同一个 ctx 重扫给演进器用——改名前的条目名从缓存里出来成了"种子声明了、
    # 盘上没有"的幻影，改名后的真文件反倒成了本地文件：同一集两份、外加一条未改名。
    # 幻影一旦被判重 trash，就是 critic N1 那种丢种子记录、`trash_path` 为空的记录。
    # sidecar 的 `season_offsets` 缓存是模块级的，同样只活到这次扫描为止。
    ctx._tfile_cache = {}                 # type: ignore[attr-defined]
    from .plugins import builtin as _builtin
    _builtin._OFFSET_CACHE.clear()

    # --- qBittorrent：按 content_path 建索引 ---
    # 读不到（未登录 / torrents() 出错）时照常扫完磁盘——诊断仍然有用——
    # 但记进 qbit_errors：没有种子视图的快照，每个有种子的文件都会被当成纯本地
    # 文件。2026-09-19 run 20260919T225410 就是这样把归种子 d08f05a7 所有的
    # `朱音落语 S01E12.mp4` 以 `torrent_hash ""` 移进了隔离区。
    torrents: list[dict] = []
    listed = False                        # torrents() 真的成功返回过
    if ctx.qbit is None:
        state.qbit_errors.append("qBittorrent 不可用（登录失败或未配置）")
    else:
        try:
            torrents = ctx.qbit.torrents()
            listed = True
        except Exception as e:
            msg = f"torrents() 读取失败：{type(e).__name__}: {e}"
            state.qbit_errors.append(msg)
            ctx.log(f"[scan] qBittorrent 数据不完整——{msg}")
    state.torrents = torrents
    state.qbit_listed = listed
    torrent_by_hash = {t["hash"]: t for t in torrents}
    by_path: dict[str, dict] = {}
    for t in torrents:
        cp = t.get("content_path", "")
        if cp:
            by_path[cp] = t
            # 合集种子：content_path 是目录，成员文件挂在它下面
            by_path.setdefault(cp.rstrip("/"), t)

    # --- 出处账本（`ledger`）：每个种子是什么，挂到它的文件上 ---
    # 读不了（坏了、结构版本更新）不拦扫描：按没有账本走，原因记进 `ledger_problem`（健康报告说出来）
    from . import ledger as ledger_mod
    ledger_rows, state.ledger_problem = ledger_mod.load_rows(cfg.state_dir)
    state.ledger_rows = ledger_rows
    if state.ledger_problem:
        ctx.log(f"[scan] 出处账本{state.ledger_problem}；这一轮按没有账本认集位与版本")

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
        videos_of: dict[str, int] = {}   # 种子 → 要下载的视频条目数（`MediaFile.torrent_videos`）
        for h, t in torrent_by_hash.items():
            sp = (t.get("save_path") or "").rstrip("/")
            if not sp or not under(sp, show_dir):
                continue
            for entry in _torrent_files(ctx, h, state.qbit_errors):
                if entry.get("priority", 1) == 0:
                    continue            # 被标记为不下载
                abs_p = Path(sp) / entry["name"]
                suffix = abs_p.suffix.lower()
                if suffix not in VIDEO_EXTS and suffix not in SUB_EXTS:
                    continue
                if suffix in VIDEO_EXTS:
                    videos_of[h] = videos_of.get(h, 0) + 1
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
                ledger=ledger_rows.get(h.lower()),
                torrent_videos=videos_of.get(h, 0),
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
                ledger=ledger_rows.get((t.get("hash") or "").lower()) if t else None,
            ))

        if show.files or show.extras_files:
            show.is_movie = _looks_like_movie(
                show_dir, sum(1 for f in show.files if f.ext in VIDEO_EXTS))
            state.shows.append(show)

    # --- 合理性：登录成功却 0 个种子，而库里明明有视频 ---
    # `torrents()` 成功返回 `[]` 与"真的一个种子都没有"在接口上无法区分。前者的后果
    # 与 qBit 不可用一模一样：每个有种子的文件都成了纯本地文件，改名走文件系统、
    # 隔离跳过种子——就是 LAT-01（run 20260919T225410）的形态，只是没有任何报错。
    # qBittorrent 5.x 只在会话恢复完之后才起 WebUI（application.cpp 里 WebUI 建在
    # Session::restored 的回调里），"启动中返回空列表"不会发生；真正的触发是一个
    # 端着空会话的 qBit——比如容器重建时没挂上 config / BT_backup 卷。这个判据不需要
    # 跨轮状态。部分缺失（比上一轮少了一截）要和上一轮比：见下面的种子数合理性。
    if listed and not torrents and not cfg.qbit_allow_empty:
        videos = sum(1 for s in state.shows for f in (*s.files, *s.extras_files)
                     if f.ext in VIDEO_EXTS)
        if videos:
            msg = (f"qBittorrent 报告 0 个种子，而媒体库里有 {videos} 个视频文件——"
                   f"多半是 qBit 的会话没恢复（容器重建丢了 BT_backup 等）；"
                   f"库里确实不用种子的话设 QBIT_ALLOW_EMPTY=1")
            state.qbit_errors.append(msg)
            ctx.log(f"[scan] qBittorrent 数据不可信——{msg}")

    # --- 合理性：比上一轮被采信的计数少了一截，审计里又没有对应的摘除（`health` 模块文档）---
    # 只恢复了一部分会话的 qBit 照样成功返回，接口上没有任何报错；后果与上面一样。
    if listed and torrents and not state.qbit_errors:
        from .health import torrent_count_problem
        msg = torrent_count_problem(cfg, len(torrents))
        if msg:
            state.qbit_errors.append(msg)
            ctx.log(f"[scan] qBittorrent 数据不可信——{msg}")

    # --- 不在 Media 下的种子（下载中/别处）---
    root_str = str(media_root)
    state.orphan_torrents = [
        t for t in torrents if not (t.get("content_path", "") or "").startswith(root_str)
    ]

    if resolve_tmdb and ctx.tmdb and ctx.tmdb.enabled:
        _resolve_tmdb(ctx, state)

    return state


def attach_ledger(state: LibraryState, rows: dict, problem: str = "") -> None:
    """把（补录之后）重新读到的账本挂回这一轮的文件上：`run` 开头的增量补录在扫描之后才做。"""
    state.ledger_rows, state.ledger_problem = rows, problem
    for s in state.shows:
        for f in (*s.files, *s.extras_files):
            f.ledger = rows.get(f.torrent_hash.lower()) if f.torrent_hash else None


def _id_key(tmdb_id: int) -> str:
    """TMDB 条目元数据（标题、季）在缓存里的键：**按 tmdb_id**。以前按目录名——目录一改名就换了键、
    重新搜一遍，搜出来的可能是另一个条目（critic N4）。"""
    return f"tmdbshow:{tmdb_id}"


def _resolve_tmdb(ctx: Context, state: LibraryState) -> None:
    """给每部番挂上 TMDB 身份、季信息与**稳定后的**标题。

    身份（tmdb_id）：
    - sidecar 里有 `tmdb_id` 就照它认，不搜——那是钉住的身份（代码只在没有时填一次，之后只有人改）。
      LAT-04：缓存过期后按目录名重新搜，搜不到时标题退回目录名、文件被改回去；搜到另一个条目时
      `终物语 下` 的 19 个文件被改成了 `物语系列 …`。
    - 没钉住的才搜：旧的按目录名缓存 → 搜目录名 / AB 标题（搜不到负缓存 `TMDB_MISS_TTL`）→ 确定性规则
      选一个。规则选不出、要问模型的，**这一轮不用**：记进 `state.tmdb_proposals`，由 `tmdb-identity`
      检测器提议 `pin_tmdb` 动作钉进 sidecar（审计里看得见、能回退），下一轮起照钉住的认。以前模型每轮
      重新选、谁也看不见，直接决定改名目标、目录名、分类（critic N4）。
    元数据按 tmdb_id 缓存（`TMDB_TTL`）；取不到新的就用旧的（不看时效）兜底。
    标题过 `titles` 的稳定闸：取不到不退回目录名，变了要连看两轮，30 天内不改回去。
    """
    from . import sidecar as sc_mod
    from . import titles
    from .cache import Cache

    cache = Cache(ctx.config.cache_db)
    book, problem = titles.load(ctx.config.state_dir)
    if problem:
        ctx.log(f"[scan] 标题稳定记录{problem}：这一轮按没有记录处理（以 sidecar 里的标题为已采用）")
    metas: dict[int, dict | None] = {}
    # 这一轮 TMDB 出过错：其余的不再打网络，用缓存兜底。"这一轮"是这个 Context——`run` 迭代到不动点时每次迭代重扫一次，
    # 断路器按扫描算的话 TMDB 挂着时每次迭代都再等一次 20 秒超时（`converge`）
    net = {"broken": str(getattr(ctx, "tmdb_scan_down", "") or "")}
    if net["broken"]:
        ctx.log(f"[scan] TMDB 这一轮前面出过错（{net['broken']}），这次扫描不再打网络、用缓存兜底")

    for show in state.shows:
        sc, corrupt = sc_mod.load_checked(show.dir_path)
        if corrupt:
            # 身份认不准：不搜（搜出来的可能是另一个条目），也不按任何标题改名。坏档案由 sidecar-sync 报
            show.naming_hold = f"sidecar 解析不了（{corrupt}），TMDB 身份认不准"
            continue
        if sc.tmdb_id:
            try:
                tid = int(sc.tmdb_id)
            except (TypeError, ValueError):
                show.naming_hold = f"sidecar 的 tmdb_id 不是整数：{sc.tmdb_id!r}"
                continue
            source = "sidecar"
        else:
            tid = _search_tmdb(ctx, cache, state, show, net)
            source = "search"
            if not tid:
                continue

        if tid not in metas:
            metas[tid] = _tmdb_meta(ctx, cache, tid, show.dir_name, net)
        meta = metas[tid]
        stale = meta or cache.get_tmdb_stale(_id_key(tid)) or {}
        show.tmdb_id = tid
        show.tmdb_source = source
        show.tmdb_seasons = stale.get("seasons", [])
        if "tmdb_title" in (sc.pinned or []) and sc.tmdb_title:
            show.tmdb_title = sc.tmdb_title          # 人钉住的标题：不过稳定闸、不记
            continue
        if tid not in state.title_decisions:         # 一个条目一个决定（物语系列 14 个目录共用一个）
            fallback = (sc.tmdb_title if str(sc.tmdb_id) == str(tid) else "") or stale.get("title", "")
            state.title_decisions[tid] = book.decide(tid, (meta or {}).get("title") or None,
                                                     fallback=fallback)
        show.tmdb_title = state.title_decisions[tid].title
        if not show.tmdb_title:
            show.naming_hold = (f"TMDB 条目 {tid} 这一轮取不到标题（{net['broken'] or '没有缓存'}），"
                                f"也没有记录过的标题")
    if net["broken"]:
        ctx.tmdb_scan_down = net["broken"]


def _tmdb_meta(ctx: Context, cache, tid: int, dir_name: str, net: dict) -> dict | None:
    """这个 tmdb_id 的 {title, seasons}：按 id 的缓存 → 旧的按目录名缓存（id 对得上就迁过来，
    部署后第一轮不必把 100 多部番重查一遍）→ 问 TMDB。取不到返回 None（调用方用旧值兜底）。"""
    fresh = cache.get_tmdb(_id_key(tid))
    if fresh:
        return fresh
    legacy = cache.get_tmdb(dir_name)
    if legacy and legacy.get("id") == tid and legacy.get("title"):
        meta = {"title": legacy["title"], "seasons": legacy.get("seasons", [])}
        cache.put_tmdb(_id_key(tid), meta)
        return meta
    if net["broken"]:
        return None
    from .clients import seasons_of, title_of
    try:
        d = ctx.tmdb.tv_detail(tid)
        title, seasons = title_of(d)[0], seasons_of(d)
    except Exception as e:
        net["broken"] = f"{type(e).__name__}: {e}"
        ctx.log(f"[scan] TMDB 取条目 {tid} 的标题 / 季信息失败（{net['broken']}），用上次的；"
                f"这一轮其余的也不再问 TMDB")
        return None
    meta = {"title": title, "seasons": seasons}
    cache.put_tmdb(_id_key(tid), meta)
    return meta


def _search_tmdb(ctx: Context, cache, state: LibraryState, show: Show, net: dict) -> int | None:
    """没钉住身份的番：找它的 tmdb_id。找到返回 id；搜不到、或要问模型的（进 `state.tmdb_proposals`，
    这一轮不用）返回 None。"""
    from .cache import LOOKUP_TTL, TMDB_MISS_TTL

    legacy = cache.get_tmdb(show.dir_name)
    if legacy and legacy.get("id"):
        return int(legacy["id"])

    # 查询词优先用 AutoBangumi 的 official_title / title_raw，比目录名更干净
    queries = [show.dir_name]
    if show.bangumi:
        for k in ("official_title", "title_raw"):
            v = (show.bangumi.get(k) or "").strip()
            if v and v not in queries:
                queries.append(v)

    picked = cache.get_llm(f"tmdbpick:{show.dir_name}", ttl=LOOKUP_TTL)
    if picked and picked.get("id"):
        state.tmdb_proposals.append({**picked, "show": show.dir_name, "show_dir": str(show.dir_path)})
        return None
    miss_key = "tmdbmiss:" + "|".join(queries)
    if cache.get_tmdb(miss_key, ttl=TMDB_MISS_TTL) or net["broken"]:
        return None

    for q in queries:
        try:
            results = ctx.tmdb.search_tv(q)
        except Exception as e:
            net["broken"] = f"{type(e).__name__}: {e}"
            ctx.log(f"[scan] TMDB 查询失败 {q}（{net['broken']}），这一轮其余的也不再问 TMDB")
            return None
        if not results:
            continue
        hit, how, why = _pick_tmdb(ctx, show, q, results)
        if hit and how == "llm":
            prop = {"id": int(hit["id"]), "title": hit.get("name") or hit.get("original_name") or "",
                    "query": q, **why,
                    "candidates": [{"id": r["id"], "name": r.get("name"),
                                    "first_air_date": r.get("first_air_date")} for r in results[:8]]}
            cache.put_llm(f"tmdbpick:{show.dir_name}", prop)
            state.tmdb_proposals.append({**prop, "show": show.dir_name, "show_dir": str(show.dir_path)})
            return None
        if how == "llm":
            break                           # 问过模型、它也选不出：负缓存，别每轮再问
        if hit:
            from .clients import seasons_of, title_of
            try:
                d = ctx.tmdb.tv_detail(hit["id"])
                title, seasons = title_of(d)[0], seasons_of(d)
            except Exception as e:
                net["broken"] = f"{type(e).__name__}: {e}"
                ctx.log(f"[scan] TMDB 取标题 / 季信息失败 {show.dir_name}（id {hit['id']}），这一轮按没匹配处理："
                        f"{net['broken']}")
                return None
            # 旧格式的按目录名缓存照写一份：回退到上一个版本时它还认这个键
            cache.put_tmdb(show.dir_name, {"id": hit["id"], "title": title, "seasons": seasons})
            cache.put_tmdb(_id_key(int(hit["id"])), {"title": title, "seasons": seasons})
            return int(hit["id"])
    cache.put_tmdb(miss_key, {"queries": queries})
    return None


def _pick_tmdb(ctx: Context, show: Show, query: str,
               results: list[dict]) -> tuple[dict | None, str, dict]:
    """多个候选时选哪个 —— 确定性规则先行，仍模糊才问模型。返回 (选中的, 怎么选的, 模型的理由)：
    怎么选的 = single / exact / first（没开模型时的旧兜底）/ llm（问了模型，选中的可能为 None）。
    模型选的调用方不直接用（见 `_resolve_tmdb`）。"""
    if len(results) == 1:
        return results[0], "single", {}

    from .naming import normalize
    q = normalize(query)
    exact = [r for r in results
             if normalize(r.get("name", "")) == q or normalize(r.get("original_name", "")) == q]
    if len(exact) == 1:
        return exact[0], "exact", {}

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
        why = {"confidence": (ans or {}).get("confidence"),
               "reason": str((ans or {}).get("reason") or "")[:200]}
        try:
            conf = float((ans or {}).get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0                   # 模型没按格式给置信度：当作不确定
        if ans and ans.get("id") and conf >= 0.7:
            for r in results:
                if r["id"] == ans["id"]:
                    return r, "llm", why
        return None, "llm", why

    return (results[0], "first", {}) if exact else (None, "", {})
