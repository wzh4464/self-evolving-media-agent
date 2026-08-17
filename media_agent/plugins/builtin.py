"""内置检测器。每一条都对应一次真实踩过的坑，注释里写明出处。

注册顺序 = 优先级：同一文件被多条规则命中时，先注册的赢（Registry 内去重）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from ..cache import Cache
from ..dedup import content_digest
from ..kernel import Action, Context, Finding, LibraryState, MediaFile, Registry, Show
from ..naming import (
    SUB_EXTS, VIDEO_EXTS, is_extra, is_normalized, normalize, parse_episode,
    parse_quality, subtitle_lang_tag, target_filename, target_subtitle_filename,
)


def _season_of(f: MediaFile, show: Show, parsed_season: int | None) -> int:
    """确定季号。优先级：文件名显式 Sxx > 目录 `Season N` > AutoBangumi 记录 > 1。"""
    if parsed_season is not None:
        return parsed_season
    m = re.match(r"Season\s+(\d+)", f.season_dir, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if show.bangumi and show.bangumi.get("season"):
        return int(show.bangumi["season"])
    return 1


def _apply_offset(ep: int, show: Show) -> int:
    """绝对集号 → 季内集号。

    出处：《超超超超超喜欢你的100个女朋友》第三季用绝对集号 25-36 发布，
    实际是 S03E01-E12，靠 AutoBangumi 的 episode_offset=-24 换算。
    """
    if show.bangumi:
        off = show.bangumi.get("episode_offset") or 0
        if off:
            return ep + int(off)
    return ep


def _resolve(f: MediaFile, show: Show) -> tuple[int, int] | None:
    """解析出 (season, episode)，失败返回 None。

    **文件名优先，种子名只作兜底。** 合集种子（一个种子含整季）的 `torrent_name`
    对所有成员文件都相同（形如 `- 01-12 -`），拿它做逐文件集号识别会把整季
    误判成同一集的重复——实测差点导致 12 集正片被当重复删掉。
    """
    season, ep = parse_episode(f.filename)
    if ep is None and f.torrent_name:
        season, ep = parse_episode(f.torrent_name)
    if ep is None:
        return None
    return _season_of(f, show, season), _apply_offset(ep, show)


def _is_video(f: MediaFile) -> bool:
    return f.ext in VIDEO_EXTS or (f.ext == ".!qB" and Path(f.path.stem).suffix.lower() in VIDEO_EXTS)


# ---------------------------------------------------------------------------
class OrphanTorrentDetector:
    """在 Media 目录里但没有 `ab:<id>` 标签的种子。

    出处：手动用 qBittorrent API 加种子会绕过 AutoBangumi 的下载器，
    而 `ab:` 标签只在它自己的 `add_torrent()` 里打——没标签 = 永远不会被自动改名。
    这是本项目存在的根本原因。
    """
    id = "orphan-torrent"
    kind = "orphan_torrent"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        by_dir = {s.dir_name: s for s in state.shows}
        bangumi_id: dict[str, int] = {}
        for s in state.shows:
            if s.bangumi:
                bangumi_id[s.dir_name] = s.bangumi["id"]

        seen: set[str] = set()
        for show in state.shows:
            bid = bangumi_id.get(show.dir_name)
            if not bid:
                continue
            for f in show.files:
                if not f.torrent_hash or f.torrent_hash in seen:
                    continue
                if re.search(r"\bab:\d+", f.torrent_tags or ""):
                    continue
                seen.add(f.torrent_hash)
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=f"种子缺少 ab:{bid} 标签，脱离 AutoBangumi 自动改名管辖",
                    show=show.dir_name, path=str(f.path), torrent_hash=f.torrent_hash,
                    evidence={"current_tags": f.torrent_tags, "expected": f"ab:{bid}"},
                    action=Action(op="retag",
                                  args={"torrent_hash": f.torrent_hash, "tags": f"ab:{bid}"},
                                  note="补标签后 AutoBangumi 的维护线程才能识别"),
                )


# ---------------------------------------------------------------------------
class UnrenamedDetector:
    """磁盘文件名不符合 `{official_title} SxxExx.ext` 规范。

    出处：判断依据必须是**磁盘文件名**——qBittorrent 的 `name` 字段在
    renameFile 之后不更新，用它判断会产生上百条误报。
    归一化比较（全角/半角、大小写）也是必须的：`Re：` vs `Re:`、`GNOSIA` vs `Gnosia`
    都曾被误判成未改名。
    """
    id = "unrenamed-file"
    kind = "unrenamed"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            title = show.official_title
            for f in show.files:
                if f.is_incomplete:
                    continue                       # 下载中，等它完成
                if is_extra(f.filename):
                    continue                       # 交给 ExtrasDetector
                if is_normalized(f.filename, title):
                    continue
                if f.ext not in VIDEO_EXTS and f.ext not in SUB_EXTS:
                    continue

                resolved = _resolve(f, show)
                if not resolved:
                    yield Finding(
                        rule=self.id, kind="unparsable", severity="minor",
                        summary=f"无法解析集号，需人工或模型判断：{f.filename}",
                        show=show.dir_name, path=str(f.path),
                        torrent_hash=f.torrent_hash,
                        evidence={"torrent_name": f.torrent_name},
                    )
                    continue

                season, ep = resolved
                if f.ext in SUB_EXTS:
                    lang = subtitle_lang_tag(f.filename)
                    new = (target_subtitle_filename(title, season, ep, lang, f.ext)
                           if lang else target_filename(title, season, ep, f.ext))
                else:
                    new = target_filename(title, season, ep, f.ext)

                if new == f.filename:
                    continue
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=f"{f.filename} → {new}",
                    show=show.dir_name, path=str(f.path), torrent_hash=f.torrent_hash,
                    evidence={"season": season, "episode": ep, "official_title": title},
                    action=Action(op="rename",
                                  args={"path": str(f.path), "new_name": new,
                                        "torrent_hash": f.torrent_hash}),
                )


# ---------------------------------------------------------------------------
class DuplicateEpisodeDetector:
    """同一 (季, 集) 存在多个文件。

    取舍规则（实测确立）：1080p > 720p，简体 > 繁体，BDRip > WebRip，同档比体积。
    **删除前必须用内容哈希确认两者确非同一份**——若哈希相同说明是同一内容的
    冗余副本，删任意一份都安全；若不同则是不同版本，按画质规则取舍。
    """
    id = "duplicate-episode"
    kind = "duplicate"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        cache = Cache(ctx.config.cache_db)
        for show in state.shows:
            buckets: dict[tuple[int, int], list[MediaFile]] = defaultdict(list)
            for f in show.files:
                if not _is_video(f) or f.is_incomplete or is_extra(f.filename):
                    continue
                r = _resolve(f, show)
                if r:
                    buckets[r].append(f)

            for (season, ep), files in sorted(buckets.items()):
                if len(files) < 2:
                    continue

                # 安全闸：同一集出现 >3 个文件，几乎必然是集号解析错了
                # （合集种子、`[S00E01]` 这类畸形命名都会造成整桶塌缩），
                # 此时绝不产出删除动作，只报可疑，交给人或演进器去理解。
                if len(files) > 3:
                    yield Finding(
                        rule=self.id, kind="suspicious_episode_parse", severity="minor",
                        summary=(f"S{season:02d}E{ep:02d} 竟有 {len(files)} 个文件声称是同一集，"
                                 f"判定为集号解析异常而非重复，已跳过删除"),
                        show=show.dir_name, path=str(files[0].path),
                        evidence={"count": len(files),
                                  "files": [f.filename for f in files[:8]]},
                    )
                    continue

                ranked = sorted(
                    files,
                    key=lambda f: parse_quality(f.torrent_name or f.filename, f.size).rank(),
                    reverse=True,
                )
                keeper, losers = ranked[0], ranked[1:]
                kd = content_digest(keeper.path, cache)
                for loser in losers:
                    ld = content_digest(loser.path, cache)
                    identical = bool(kd and ld and kd == ld)
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        summary=(f"S{season:02d}E{ep:02d} 重复：保留 {keeper.filename}，"
                                 f"清理 {loser.filename}"),
                        show=show.dir_name, path=str(loser.path),
                        torrent_hash=loser.torrent_hash,
                        evidence={
                            "keep": str(keeper.path), "keep_size": keeper.size,
                            "drop_size": loser.size,
                            "keep_digest": kd, "drop_digest": ld,
                            "byte_identical": identical,
                            "reason": "字节完全相同" if identical else "同集不同版本，按画质取舍",
                        },
                        action=Action(op="trash", reversible=True,
                                      args={"path": str(loser.path),
                                            "torrent_hash": loser.torrent_hash},
                                      note="移入隔离区，保留期内可恢复"),
                    )


# ---------------------------------------------------------------------------
class RenameCollisionDetector:
    """多个种子的目标文件名相同 —— AutoBangumi 会陷入每分钟重试的死循环。

    出处：《朱音落语》E08/E09 的 JPSC 与 JPTC 两个版本集号相同，
    都想改名成 `朱音落语 S01E08.mp4`，日志里每 60 秒重复一次、永不收敛。
    """
    id = "rename-collision"
    kind = "rename_collision"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            targets: dict[str, list[MediaFile]] = defaultdict(list)
            for f in show.files:
                if not _is_video(f) or is_extra(f.filename):
                    continue
                r = _resolve(f, show)
                if not r:
                    continue
                season, ep = r
                targets[target_filename(show.official_title, season, ep, f.ext)].append(f)

            for target, files in targets.items():
                if len(files) < 2:
                    continue
                hashes = [f.torrent_hash for f in files if f.torrent_hash]
                if len(set(hashes)) < 2:
                    continue      # 同一个种子的多个文件，不是碰撞
                yield Finding(
                    rule=self.id, kind=self.kind, severity="critical",
                    summary=f"{len(files)} 个种子争抢同一目标名 {target}，会导致改名死循环",
                    show=show.dir_name, path=str(files[0].path),
                    evidence={"target": target,
                              "competitors": [
                                  {"file": f.filename, "hash": f.torrent_hash,
                                   "size": f.size} for f in files]},
                    # 具体保留哪个交给 DuplicateEpisodeDetector 的画质规则，这里只报警
                )


# ---------------------------------------------------------------------------
class DeadTorrentDetector:
    """0 做种 + availability 0 + 长期停滞 = 死种，等下去也不会有进度。

    出处：《异世界四重奏》S02 整季 —— 所有 tracker 都报 seeds=0，
    availability=0 表示全网 peer 拼不出一份完整文件，换源也无解。
    """
    id = "dead-torrent"
    kind = "dead_torrent"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        import time
        threshold = ctx.config.dead_torrent_hours * 3600
        now = time.time()
        for t in state.torrents:
            if t.get("progress", 0) >= 1.0:
                continue
            if t.get("state") not in ("stalledDL", "downloading", "metaDL"):
                continue
            if t.get("num_complete", 0) > 0 or t.get("num_seeds", 0) > 0:
                continue
            if t.get("availability", 0) > 0:
                continue
            stalled = now - (t.get("added_on") or now)
            if stalled < threshold:
                continue
            yield Finding(
                rule=self.id, kind=self.kind, severity="important",
                summary=f"死种（全网无完整副本，已停滞 {stalled/3600:.0f}h）：{t.get('name','')[:60]}",
                path=t.get("content_path", ""), torrent_hash=t.get("hash", ""),
                evidence={"num_seeds": t.get("num_seeds"),
                          "num_complete": t.get("num_complete"),
                          "availability": t.get("availability"),
                          "progress": t.get("progress"),
                          "stalled_hours": round(stalled / 3600)},
                action=Action(op="trash", reversible=True,
                              args={"torrent_hash": t.get("hash", ""),
                                    "path": t.get("content_path", "")},
                              note="删除种子；已下载的碎片进隔离区"),
            )


# ---------------------------------------------------------------------------
class ExtrasDetector:
    """菜单 / PV / NCOP / NCED / 特典等周边内容。

    出处：TMDB 不把这些收录成 episode（实测《100个女朋友》《令和妖神斑小姐》
    都没有 Season 0），放在库里既刮不到元数据又污染剧集列表。
    """
    id = "extras-in-library"
    kind = "extra_content"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            for f in show.files:
                if not is_extra(f.filename):
                    continue
                if not _is_video(f):
                    continue
                yield Finding(
                    rule=self.id, kind=self.kind, severity="minor",
                    summary=f"特典/周边内容，TMDB 无对应条目：{f.filename}",
                    show=show.dir_name, path=str(f.path), torrent_hash=f.torrent_hash,
                    evidence={"size": f.size},
                    action=Action(op="trash", reversible=True,
                                  args={"path": str(f.path),
                                        "torrent_hash": f.torrent_hash,
                                        "file_only": True},
                                  note="种子内其余正片保留，仅该文件设为不下载并移入隔离区"),
                )


# ---------------------------------------------------------------------------
class TitleDriftDetector:
    """目录名与 TMDB 官方标题不一致 —— 会导致刮削失败或匹配错剧。

    出处：`银魂番外` → TMDB 实为《3年Z组银八老师》；
    `Akane-banashi` → TMDB 中文名《朱音落语》。
    """
    id = "title-drift"
    kind = "title_drift"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            if not show.tmdb_id or not show.tmdb_title:
                continue
            if normalize(show.dir_name) == normalize(show.tmdb_title):
                continue
            yield Finding(
                rule=self.id, kind=self.kind, severity="important",
                summary=f"目录名 `{show.dir_name}` 与 TMDB 官方标题 `{show.tmdb_title}` 不一致",
                show=show.dir_name, path=str(show.dir_path),
                evidence={"tmdb_id": show.tmdb_id, "tmdb_title": show.tmdb_title,
                          "file_count": len(show.files)},
                action=Action(op="rename_show_dir",
                              args={"path": str(show.dir_path),
                                    "new_name": show.tmdb_title,
                                    "bangumi_id": (show.bangumi or {}).get("id")},
                              note="同步更新 AutoBangumi 的 save_path，避免下次新集又建旧目录"),
            )


# ---------------------------------------------------------------------------
class MissingNfoDetector:
    """TMDB 上没有中文标题的番，需要 NFO 强制指定 tmdbid 才能刮准。

    出处：《令和妖神斑小姐》TMDB 只有日文/英文标题，中文目录名匹配不上，
    靠 `tvshow.nfo` 里的 `<uniqueid type="tmdb">` 直接锁定条目。
    """
    id = "missing-nfo"
    kind = "missing_nfo"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            if not show.tmdb_id:
                continue
            if normalize(show.dir_name) == normalize(show.tmdb_title):
                continue          # 名字对得上，能自动刮到，不需要 NFO
            nfo = show.dir_path / "tvshow.nfo"
            if nfo.exists() and str(show.tmdb_id) in nfo.read_text(
                    encoding="utf-8", errors="ignore"):
                continue
            yield Finding(
                rule=self.id, kind=self.kind, severity="minor",
                summary=f"目录名与 TMDB 标题不符且无 NFO，刮削可能失败：{show.dir_name}",
                show=show.dir_name, path=str(nfo),
                evidence={"tmdb_id": show.tmdb_id, "tmdb_title": show.tmdb_title},
                action=Action(op="write_nfo",
                              args={"path": str(nfo), "tmdb_id": show.tmdb_id,
                                    "title": show.dir_name,
                                    "original_title": show.tmdb_title}),
            )


# ---------------------------------------------------------------------------
class WrongCategoryDetector:
    """qBittorrent 分类不是番名（AutoBangumi 默认全丢进 `Bangumi`）。"""
    id = "wrong-category"
    kind = "wrong_category"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        seen: set[str] = set()
        for show in state.shows:
            title = show.official_title
            for f in show.files:
                if not f.torrent_hash or f.torrent_hash in seen:
                    continue
                cat = f.torrent_category
                if not cat or cat == title:
                    continue
                if cat != "Bangumi" and normalize(cat) == normalize(show.dir_name):
                    continue      # 用的是目录名，可接受
                seen.add(f.torrent_hash)
                yield Finding(
                    rule=self.id, kind=self.kind, severity="minor",
                    summary=f"分类 `{cat}` 应为 `{title}`",
                    show=show.dir_name, path=str(f.path), torrent_hash=f.torrent_hash,
                    evidence={"current": cat, "expected": title},
                    action=Action(op="recategorize",
                                  args={"torrent_hash": f.torrent_hash,
                                        "category": title}),
                )


BUILTIN = [
    RenameCollisionDetector,     # critical 优先
    OrphanTorrentDetector,
    DuplicateEpisodeDetector,
    UnrenamedDetector,
    DeadTorrentDetector,
    TitleDriftDetector,
    WrongCategoryDetector,
    MissingNfoDetector,
    ExtrasDetector,
]


def register_builtins(registry: Registry) -> Registry:
    for cls in BUILTIN:
        registry.register(cls())
    return registry
