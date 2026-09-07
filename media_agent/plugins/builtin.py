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
from ..kernel import (Action, Context, Finding, LibraryState, MediaFile,
                      Registry, Show, tmdb_groups)
from ..probe import probe, size_for_compare
from ..naming import (
    SUB_EXTS, VIDEO_EXTS, declared_season, is_extra, is_normalized, normalize,
    parse_episode,
    parse_pin, parse_quality, season_of_dir, subtitle_lang_tag, target_filename,
    target_subtitle_filename,
)


def _season_of(f: MediaFile, show: Show, parsed_season: int | None) -> int:
    """确定季号。优先级：文件名显式 Sxx > 目录 `Season N` > AutoBangumi 记录 > 1。"""
    if parsed_season is not None:
        return parsed_season
    sn = season_of_dir(f.season_dir)
    if sn is not None:
        return sn
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


_OFFSET_CACHE: dict[str, dict] = {}



def _rank_for_keep(f: MediaFile) -> tuple:
    """重复集取舍的排序键。分数越高越该留。

    在 `parse_quality`（只看名字）之上叠一层**文件内部的事实**：

    - **字幕能力用探测的，不用猜的。** 2026-09-05 尼古喵喵 S01E08 两个候选，
      带简繁双字幕轨的那份因为内部文件名只写 `SRTx2`（"简繁内封字幕"只在
      Mikan 站点标题里）被判成"无简体"，与零字幕轨的生肉并列，最后按体积
      判输被删。打开文件看轨道就没有这个问题。
    - **体积跨编码折算。** 同画质下 HEVC/AV1 只要 AVC 六成，裸比体积等于
      系统性偏向低效编码——上面那次正是 AVC 710MB 赢了 HEVC 566MB。

    探不到（文件不在、没装 ffprobe）就原样退回纯名字判断，不制造新的失败模式。
    """
    q = parse_quality(f.torrent_name or f.filename, f.size)
    info = probe(f.path)
    if info is None:
        return (q.height, 1 if q.simplified else 0, int(q.is_bdrip), float(f.size))
    subs = info.subtitle_rank()
    # 容器里没有字幕轨、但名字明说带中文字幕 —— 多半是内嵌硬字幕。
    # 硬字幕看不见轨道，不能因此判它"没字幕"（用户：内封最好，内嵌也行）。
    if subs == 0 and (q.simplified or q.traditional):
        subs = 3 if q.simplified else 2
    return (info.height or q.height, subs, int(q.is_bdrip),
            size_for_compare(f.size, info.vcodec))


def _pinned(f: MediaFile) -> tuple[int, int] | None:
    """种子上钉死的集号标签 `ma:S01E58`。

    抓取器在下种子的那一刻就知道该集在库内的规范编号（它是按番组页 + 播出
    日期定位的）。这个信息到了改名阶段就丢了，只剩发布名里那个可能按分季
    编号的数字。把它钉在 qBittorrent 标签上，改名和判重都优先认它。

    带 `ma:` 的种子同时也被 OrphanTorrentDetector 豁免——它们不交给
    AutoBangumi 改名，因为 AB 的 `episode_offset` 是整条订阅一个值，
    表达不了"同一目录里不同来源季用不同偏移"。
    """
    return parse_pin(f.torrent_tags or "")


def _season_offsets(show: Show) -> dict:
    """sidecar 里记的 `season_offsets`：发布方季号 → 该季之前的累计集数。"""
    key = str(show.dir_path)
    if key not in _OFFSET_CACHE:
        from .. import sidecar as sc_mod
        try:
            _OFFSET_CACHE[key] = dict(sc_mod.load(show.dir_path).season_offsets or {})
        except Exception:
            _OFFSET_CACHE[key] = {}
    return _OFFSET_CACHE[key]


def _numbered_from(f: MediaFile, show: Show):
    """返回 (用于解析的原始串, season, ep)。文件名优先，种子名兜底。"""
    raw = f.filename
    season, ep = parse_episode(raw)
    if ep is None and f.torrent_name:
        raw = f.torrent_name
        season, ep = parse_episode(raw)
    return raw, season, ep


def _numbering_conflict(f: MediaFile, show: Show) -> tuple[int, int] | None:
    """发布方声明的季号与库内季号不一致、且没有换算依据 → 返回 (发布季, 库内季)。

    见 `naming.declared_season` 的注释：这是 2026-08-31 那次误删的根因。
    注意只有**还没归一化**的文件名会命中——改成 `... S01E58.mkv` 之后，
    名字里声明的就是 S01，与库内一致，不再报冲突。
    """
    if _pinned(f):
        return None
    raw, season, ep = _numbered_from(f, show)
    if ep is None:
        return None
    target = _season_of(f, show, season)
    dec = declared_season(raw)
    if dec is None or dec == target:
        return None
    if str(dec) in _season_offsets(show):
        return None
    return dec, target


def _resolve(f: MediaFile, show: Show) -> tuple[int, int] | None:
    """解析出 (season, episode)，失败返回 None。

    **文件名优先，种子名只作兜底。** 合集种子（一个种子含整季）的 `torrent_name`
    对所有成员文件都相同（形如 `- 01-12 -`），拿它做逐文件集号识别会把整季
    误判成同一集的重复——实测差点导致 12 集正片被当重复删掉。

    **发布方声明的季号与库内季号不一致时，集号不可信。** 此时要么用 sidecar
    的 `season_offsets` 换算，要么返回 None（宁可不处理，也不要把 `3rd Season
    - 08` 写成 `S01E08` 去撞 2016 年真正的第 8 集）。
    """
    pin = _pinned(f)
    if pin:
        return pin

    raw, season, ep = _numbered_from(f, show)
    if ep is None:
        return None
    target = _season_of(f, show, season)

    dec = declared_season(raw)
    if dec is not None and dec != target:
        off = _season_offsets(show).get(str(dec))
        if off is None:
            return None                  # 交给 SeasonNumberingConflictDetector 报警
        # 同一部番里两种编号习惯并存：Fyy Raws 按分季编（3rd Season - 08），
        # Dynamis One 按连续编（4th Season - 79）。用"该季之前的累计集数"
        # 当阈值区分——两种解释的取值区间不重叠。
        if int(ep) <= int(off):
            ep = int(ep) + int(off)

    return target, _apply_offset(ep, show)


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
                if _pinned(f):
                    # 集号已由 media-agent 钉死，交给 AutoBangumi 反而会被它
                    # 按发布方的分季编号改回去（Re:Zero E58 → S01E08 就是这么
                    # 丢了原片的）。这类种子由本项目自己改名。
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
    """文件名不符合 `{official_title} SxxExx.ext` 规范。

    **事实来源分两种**（见 scan.py）：
    - 有种子的内容以 `torrents/files` 为准——那是 qBittorrent 实际会写入的路径，
      renameFile 后会同步更新，且包含尚未落盘的文件。
      注意不要用 `torrents/info` 的 `name` 字段：那是种子**显示名**，
      renameFile 后不变，拿它判断会产生上百条误报（早先踩过）。
    - 无种子的纯本地文件才以磁盘为准。

    归一化比较（全角/半角、大小写）是必须的：`Re：` vs `Re:`、`GNOSIA` vs `Gnosia`
    都曾被误判成未改名。
    """
    id = "unrenamed-file"
    kind = "unrenamed"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            if show.is_movie:
                # 电影没有集号。硬要解析，每部都稳定产出一条"无法解析集号，
                # 需人工或模型判断"——本库 45 部电影就是 45 条纯噪音，
                # 还把真正需要人看的条目淹在里面。
                continue
            title = show.official_title
            for f in show.files:
                # 下载中的也要改名 —— 拿到种子就改好，不等下载完。
                # 走 qBittorrent renameFile 是安全的：它会同步处理 `.!qB` 临时文件
                # 并继续往新名字写入，不中断下载。
                # 来源是 torrents/files，文件名本身已是干净的目标名，无需裁剪后缀。
                stem = f.filename
                if is_extra(stem):
                    continue                       # 交给 ExtrasDetector
                if is_normalized(stem, title):
                    # `is_normalized` 只看形式（`标题 SxxExx.ext`），不看集号对不对。
                    # 被别人按错口径改成 `S01E08.mkv` 的文件形式上完全合规，
                    # 就这么永远卡在错误集号上——除非种子上钉了 `ma:` 集号，
                    # 那它就是权威，跟文件名不一致时必须改回来。
                    pin = _pinned(f)
                    if not (pin and parse_episode(stem)[1] not in (None, pin[1])):
                        continue
                real_ext = Path(stem).suffix.lower()
                if real_ext not in VIDEO_EXTS and real_ext not in SUB_EXTS:
                    continue

                conflict = _numbering_conflict(f, show)
                if conflict:
                    dec, tgt = conflict
                    _r, _s, raw_ep = _numbered_from(f, show)
                    yield Finding(
                        rule=self.id, kind="season_numbering_conflict",
                        severity="important", classified=True,
                        summary=(f"发布方标的是第 {dec} 季第 {raw_ep} 集，库内却是 "
                                 f"Season {tgt}——集号口径不一致，已拒绝改名。"
                                 f"在该番 sidecar 的 season_offsets 里写 "
                                 f'"{dec}": <第{dec}季之前的累计集数> 即可自动换算'),
                        show=show.dir_name, path=str(f.path),
                        torrent_hash=f.torrent_hash,
                        evidence={"declared_season": dec, "library_season": tgt,
                                  "raw_episode": raw_ep,
                                  "torrent_name": f.torrent_name},
                    )
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
                if real_ext in SUB_EXTS:
                    lang = subtitle_lang_tag(stem)
                    new = (target_subtitle_filename(title, season, ep, lang, real_ext)
                           if lang else target_filename(title, season, ep, real_ext))
                else:
                    new = target_filename(title, season, ep, real_ext)

                if new == stem:
                    continue
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"{stem} → {new}"
                             + ("（下载中，改名不中断下载）" if f.is_incomplete else "")),
                    show=show.dir_name, path=str(f.path), torrent_hash=f.torrent_hash,
                    evidence={"season": season, "episode": ep, "official_title": title,
                              "downloading": f.is_incomplete},
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
            if show.is_movie:
                continue
            buckets: dict[tuple[int, int], list[MediaFile]] = defaultdict(list)
            for f in show.files:
                if not _is_video(f) or f.is_incomplete or is_extra(f.filename):
                    continue
                r = _resolve(f, show)
                if r:
                    buckets[r].append(f)

            for (season, ep), files in sorted(buckets.items()):
                # 同一磁盘路径只能算一份。上游 `scan` 已经按路径收敛过，
                # 这里是兜底：**删除是不可逆的，不能指望上游永远不出错。**
                #
                # 2026-09-06 尼古喵喵 S01E08 的教训——两个种子宣称同一路径，
                # 桶里进了两条实为同一文件的条目，排序后"清理输的那个"
                # 删掉的正是唯一的真文件，审计里留下 `保留 X，清理 X`。
                # 只要 keeper 和 loser 可能指向同一个路径，这条规则就有能力
                # 把一集彻底抹掉，所以护栏必须在产出删除动作之前。
                seen: set[str] = set()
                deduped = []
                for f in files:
                    key = str(f.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(f)
                files = deduped

                if len(files) < 2:
                    continue

                # 归属权还没交接完的，不做不可逆的删除。
                #
                # qBittorrent 的**分类**是本项目与 AutoBangumi 的所有权边界：
                # AB 的改名线程扫的是 `category="Bangumi" 且已完成` 的种子
                # （标签它不看），分类一旦改成剧名，它就再也看不见这个种子。
                # 所以还挂在 `Bangumi` 下的文件，名字的最终裁量权仍在 AB 手上——
                # 此刻它叫什么只是"AB 认为的"，不是定论。
                #
                # 2026-08-31：AB 把 `3rd Season - 08` 改成 `S01E08`，与 2016 年
                # 真正的第 8 集撞进同一个桶，判重按画质把 1.31GB 的原片清进了隔离区。
                # 等分类交接完再判，这一集就不会进桶。
                pending = [f for f in files if f.torrent_category == "Bangumi"]
                if pending:
                    yield Finding(
                        rule=self.id, kind="pending_ownership", severity="minor",
                        classified=True,
                        summary=(f"S{season:02d}E{ep:02d} 有 {len(files)} 个候选，但其中 "
                                 f"{len(pending)} 个仍在 AutoBangumi 的分类下"
                                 f"（改名权未交接），本轮不做取舍"),
                        show=show.dir_name, path=str(files[0].path),
                        evidence={"files": [f.filename for f in files],
                                  "pending": [f.filename for f in pending]},
                    )
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

                ranked = sorted(files, key=_rank_for_keep, reverse=True)
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
class CollidingTorrentDetector:
    """两个种子盯着**同一个文件路径**——其中一个永远下不完。

    出处：本 session 手工清理过两次，同一形态：

    | 集 | 完成的 | 卡住的 |
    |---|---|---|
    | Re:Zero E54 | `- 54v2` 100% | `- 54` 卡住 |
    | 穹庐下的魔女 E09 | `- 09v2` 100% | `- 09` 97.8% stalledDL |

    v2 是修正版，与原版**输出同名文件、内容不同**。改名归位后两个种子的
    content_path 撞到一起：v2 先下完把文件写成了它的版本，原版剩下的那几个
    分片再怎么校验也过不去——它要的那几个字节已经不在那儿了。

    所以这**不是种源问题**。当时的表象是"97.8% stalledDL、种子数很少"，
    很容易顺着"换个源/等做种"查下去，但换多少源都没用。判据是路径撞车，
    不是速度和 peer 数。

    只认 content_path 指向**视频文件**的情况：多文件种子用 NoSubfolder 布局时
    content_path 就是 Season 目录本身，一个季度目录下几十个种子共用它是常态，
    拿来当撞车会全是误报。
    """
    id = "colliding-torrent"
    kind = "torrent_path_collision"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        by_path: dict[str, list[dict]] = defaultdict(list)
        for t in state.torrents:
            cp = (t.get("content_path") or "").rstrip("/")
            if cp and Path(cp).suffix.lower() in VIDEO_EXTS:
                by_path[cp].append(t)

        for cp, group in sorted(by_path.items()):
            if len(group) < 2:
                continue
            stuck = [t for t in group if t.get("progress", 0) < 1.0]
            if not stuck:
                continue        # 都完成了：同一份数据两条记录，不影响任何人
            done = [t for t in group if t.get("progress", 0) >= 1.0]
            names = [t.get("name", "")[:70] for t in group]

            if not done:
                # 谁也没下完，纯粹在互相打架。留给人判断保哪个——
                # 这里没有"哪个是对的"的依据，自动删任何一个都是瞎猜。
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"{len(group)} 个种子抢同一个文件且都没下完，"
                             f"互相覆盖，谁也完不成：{Path(cp).name}"),
                    path=cp,
                    evidence={"torrents": names,
                              "progress": [round(t.get("progress", 0), 3) for t in group],
                              "hint": "多半是 v2 修正版与原版并存，留一个删其余"},
                )
                continue

            keep = max(done, key=lambda t: t.get("size", 0))
            for victim in stuck:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"卡在 {victim.get('progress', 0) * 100:.1f}% 的种子与一个"
                             f"已完成的种子指向同一文件，永远下不完："
                             f"{victim.get('name', '')[:60]}"),
                    path=cp, torrent_hash=victim.get("hash", ""),
                    evidence={"stuck": victim.get("name", "")[:110],
                              "stuck_progress": round(victim.get("progress", 0), 3),
                              "stuck_state": victim.get("state"),
                              "complete": keep.get("name", "")[:110],
                              "shared_path": cp},
                    action=Action(
                        op="drop_torrent", reversible=True,
                        args={"torrent_hash": victim.get("hash", ""),
                              "name": victim.get("name", ""),
                              "path": cp,
                              "keep_hash": keep.get("hash", ""),
                              "magnet": victim.get("magnet_uri", ""),
                              "save_path": victim.get("save_path", ""),
                              "category": victim.get("category", ""),
                              "tags": victim.get("tags", "")},
                        note="只删种子记录，文件留在磁盘（完整的那份由另一个种子做种）"),
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
        groups = tmdb_groups(state.shows)
        for show in state.shows:
            if not show.tmdb_id or not show.tmdb_title:
                continue
            if normalize(show.dir_name) == normalize(show.tmdb_title):
                continue
            g = groups.get(show.tmdb_id) or {}
            if g.get("kind") == "volumes":
                # TMDB 把多部独立作品收成了一个条目（物语系列 14 个目录都是
                # "物语系列"）。这不是目录名漂移，改了反而毁掉分卷组织。
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
class CategoryConsolidationDetector:
    """同一部番的种子必须归入**同一个**分类，且该分类名 = official_title。

    出处：同一部番的不同译名会各自长成一个分类，实测 8 个目录被拆成 2-3 类：
    `黄泉使者`/`黄泉的使者`、`古诺希亚`/`古诺西亚`、`入间同学入魔了`/`入间同学入魔了！`、
    `攻壳机动队`/`攻壳机动队 THE GHOST IN THE SHELL`……
    成因是每次换个来源订阅、或按不同译名建分类，就多出一个碎片。

    **归属判定用磁盘目录，不用分类名。** 落在同一个 show 目录下的种子必然是同一部番，
    这是唯一可靠的分组依据；分类名本身正是要被修正的对象，不能拿它当分组依据。

    这里刻意不保留"分类等于目录名就放过"的宽容分支——那正是碎片长期存活的原因
    （`黄泉使者` 等于目录名，于是永远不会被修正到 TMDB 的 `黄泉的使者`）。
    """
    id = "category-consolidation"
    kind = "category_fragmented"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        # 分类 → 它当前持有的种子；用于判断合并后是否变空
        cat_members: dict[str, set[str]] = defaultdict(set)
        for t in state.torrents:
            cat_members[t.get("category") or ""].add(t.get("hash", ""))

        reassigned: set[str] = set()

        # 种子 → 分类，用于路径前缀兜底
        t_by_hash = {t.get("hash", ""): t for t in state.torrents}

        for show in state.shows:
            canonical = show.official_title
            # 该目录下每个种子当前的分类
            cur: dict[str, str] = {}
            for f in show.files:
                if f.torrent_hash:
                    cur[f.torrent_hash] = f.torrent_category or ""

            # 兜底：content_path 已成死链（文件被外部改名/移动）的种子，
            # 磁盘扫描关联不上，但路径前缀仍能证明它属于这个目录。
            # 不兜这一层的话，这类种子会永远留在旧分类里合并不掉。
            prefix = str(show.dir_path) + "/"
            for h, t in t_by_hash.items():
                if h in cur:
                    continue
                if (t.get("content_path") or "").startswith(prefix):
                    cur[h] = t.get("category") or ""

            if not cur:
                continue

            variants = sorted({c for c in cur.values() if c and c != canonical})
            for h, cat in cur.items():
                if cat == canonical or h in reassigned:
                    continue
                reassigned.add(h)
                yield Finding(
                    rule=self.id, kind=self.kind, severity="minor",
                    summary=(f"分类 `{cat or '(无)'}` → `{canonical}`"
                             + (f"（该目录共有 {len(variants) + 1} 个分类待合并）"
                                if variants else "")),
                    show=show.dir_name, torrent_hash=h,
                    evidence={"current": cat, "canonical": canonical,
                              "sibling_categories": variants,
                              "title_source": ("tmdb" if show.tmdb_title else
                                               "autobangumi" if show.bangumi else "dirname")},
                    action=Action(op="recategorize",
                                  args={"torrent_hash": h, "category": canonical}),
                )

        # 合并之后会变空的分类 + 本来就空的分类，一并清掉
        canonical_names = {s.official_title for s in state.shows}
        for cat, members in sorted(cat_members.items()):
            if not cat or cat in canonical_names:
                continue
            if members - reassigned:
                continue          # 还有种子留在这个分类里，不能删
            yield Finding(
                rule=self.id, kind="empty_category", severity="minor",
                summary=f"分类 `{cat}` 合并后已无种子，可删除",
                evidence={"had_torrents": len(members)},
                action=Action(op="delete_category", args={"category": cat},
                              note="仅删分类定义，不动任何文件"),
            )


# ---------------------------------------------------------------------------
class StaleTorrentPathDetector:
    """种子的 content_path 指向已不存在的文件 —— 做种会失效。

    出处：本项目自己的 AGENTS.md 第 3 条写着"有种子的文件改名必须走 qBittorrent API"，
    而实测库里有 18 个 Gnosia 种子正是因为当初用文件系统 `mv` 改了名，
    导致 qBittorrent 记录的路径成了死链：文件还在、种子却找不到它，
    既无法继续做种，也让所有按磁盘归属分组的规则都够不着这些种子。

    **修复靠文件大小唯一定位。** 种子记录里存着每个文件的精确字节数；
    在同一个 show 目录下按大小找，若恰好只有一个候选，那就是它——
    体积精确到字节相同而内容不同的概率可以忽略。定位后用 `renameFile`
    把种子指向新文件名，再 `recheck` 让 libtorrent 逐分片校验哈希做最终确认。
    这一整套都走 qBittorrent，不碰文件系统。

    大小有歧义或找不到候选时**只报不改**——宁可留着让人看，
    也不要把种子指到错误的文件上。
    """
    id = "stale-torrent-path"
    kind = "stale_torrent_path"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        root = str(ctx.config.media_root)
        size_index: dict[str, dict[int, list[str]]] = {}

        for t in state.torrents:
            cp = t.get("content_path") or ""
            if not cp.startswith(root) or Path(cp).exists():
                continue
            # 还没下完的种子文件本来就还不在磁盘上（或带 .!qB 后缀），
            # 那是正常的下载中状态，不是失联。
            if t.get("progress", 0) < 1.0 or Path(cp + ".!qB").exists():
                continue

            show = cp[len(root):].lstrip("/").split("/")[0]
            show_dir = Path(root) / show

            # "大小 → 文件"索引。show 目录还在就只索引它（范围小、更安全）；
            # 目录本身已经不存在（被改名/合并掉了）则退化为全库索引——
            # 文件很可能已经搬去了新目录，只在原地找必然找不到。
            key = show if show_dir.is_dir() else "*ALL*"
            if key not in size_index:
                scope = show_dir if key != "*ALL*" else Path(root)
                idx: dict[int, list[str]] = {}
                for p in scope.rglob("*"):
                    if not p.is_file() or p.name.startswith("._"):
                        continue
                    if ".autobangumi" in p.parts:
                        continue
                    try:
                        idx.setdefault(p.stat().st_size, []).append(str(p))
                    except OSError:
                        pass
                size_index[key] = idx
            idx = size_index[key]

            save_path = (t.get("save_path") or "").rstrip("/")
            mapping, unresolved, matched_dirs = [], [], set()
            try:
                entries = [e for e in ctx.qbit.files(t["hash"])
                           if e.get("priority", 1) != 0]
            except Exception:
                entries = []

            for e in entries:
                # 已经正确关联的文件不用动
                if (Path(save_path) / e["name"]).exists():
                    continue
                cands = idx.get(e["size"], [])
                if len(cands) == 1:
                    hit = Path(cands[0])
                    matched_dirs.add(str(hit.parent))
                    mapping.append({"old": e["name"], "new": hit.name,
                                    "size": e["size"], "abs": str(hit)})
                elif len(cands) > 1:
                    unresolved.append((e["name"], f"{len(cands)} 个同样大小的候选，无法确定"))
                else:
                    unresolved.append((e["name"], "找不到大小匹配的文件"))

            # 文件若已搬到别的目录，光改名不够，还要把 save_path 挪过去。
            # 多个文件分散在不同目录时不敢自动处理（种子内部结构无从还原）。
            new_save_path = ""
            if len(matched_dirs) == 1:
                only = matched_dirs.pop()
                if only != save_path:
                    new_save_path = only
            elif len(matched_dirs) > 1:
                unresolved.append(("(整体)", f"匹配到的文件分散在 {len(matched_dirs)} 个目录，不敢自动重定位"))

            base = dict(state=t.get("state"), progress=t.get("progress"),
                        resolved=len(mapping), unresolved=len(unresolved),
                        unresolved_detail=unresolved[:3])

            if mapping and not unresolved:
                where = (f"（并重定位到 {Path(new_save_path).parent.name}/"
                         f"{Path(new_save_path).name}）" if new_save_path else "")
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"种子路径失效，已按文件大小唯一定位："
                             f"{Path(mapping[0]['new']).name[:40]}{where}"),
                    show=show, path=cp, torrent_hash=t["hash"],
                    evidence={**base, "mapping": mapping[:3],
                              "new_save_path": new_save_path},
                    action=Action(op="relink_torrent",
                                  args={"torrent_hash": t["hash"], "mapping": mapping,
                                        "new_save_path": new_save_path},
                                  note="setLocation + renameFile 重建关联后 recheck 校验分片哈希"),
                )
            else:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"种子路径失效且无法自动定位（{len(unresolved)} 个文件对不上）："
                             f"{Path(cp).name[:44]}"),
                    show=show, path=cp, torrent_hash=t["hash"],
                    evidence={**base,
                              "hint": "当初应走 qBittorrent renameFile 而非文件系统 mv"},
                    # 定位不了就不给 action——指错文件比不修更糟
                )


BUILTIN = [
    RenameCollisionDetector,     # critical 优先
    StaleTorrentPathDetector,
    OrphanTorrentDetector,
    DuplicateEpisodeDetector,
    UnrenamedDetector,
    DeadTorrentDetector,
    CollidingTorrentDetector,
    TitleDriftDetector,
    CategoryConsolidationDetector,
    MissingNfoDetector,
    ExtrasDetector,
]


def register_builtins(registry: Registry) -> Registry:
    from .grab import GRAB_DETECTORS
    from .sidecar_sync import SIDECAR_DETECTORS
    from .subscription import SUBSCRIPTION_DETECTORS
    # 订阅健康度规则排在最前：订阅本身失效时，下游一切规则都无从谈起。
    # 抓取器紧随其后——先补齐缺的集，后面的改名/归类规则才有东西可处理。
    # sidecar 同步放最后，记录本轮结束后的最终状态。
    for cls in (SUBSCRIPTION_DETECTORS + GRAB_DETECTORS + BUILTIN
                + SIDECAR_DETECTORS):
        registry.register(cls())
    return registry
