"""内置检测器。每一条都对应一次真实踩过的坑，注释里写明出处。

注册顺序 = 优先级：同一文件被多条规则命中时，先注册的赢（Registry 内去重）。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .. import preferences
from ..cache import Cache
from ..dedup import content_digest
from ..kernel import (Action, Context, Finding, LibraryState, MediaFile,
                      Registry, Show, tmdb_groups)
from ..probe import MediaInfo, probe, size_for_compare
from ..naming import (
    SUB_EXTS, VIDEO_EXTS, apply_episode_offset, declared_season, is_extra_of,
    is_normalized, normalize, parse_episode,
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


def _episode_offset(show: Show) -> int:
    """AutoBangumi 订阅行上的 `episode_offset`；没有订阅或为 0 时返回 0。"""
    return int((show.bangumi or {}).get("episode_offset") or 0)


def _apply_offset(raw: str, ep: int, show: Show) -> int | None:
    """绝对集号 → 季内集号；**只换算发布名里的原始集号**（`naming.apply_episode_offset`）。

    出处：《超超超超超喜欢你的100个女朋友》第三季用绝对集号 25-36 发布，
    实际是 S03E01-E12，靠 AutoBangumi 的 episode_offset=-24 换算。以前连已经
    规范成 `S03E01` 的名字也再减一次，落进 `(3, -23)`（2026-09-26 修）。
    """
    return apply_episode_offset(raw, ep, _episode_offset(show))


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
    # 硬字幕看不见轨道，不能因此判它"没字幕"，但也**不能和探到的内封轨同分**：
    # 用户口径是「内封最好，内嵌也行」，同分就把这个偏好抹平了。
    if subs == MediaInfo.NONE and (q.simplified or q.traditional):
        subs = MediaInfo.HARD_SIMPLIFIED if q.simplified else MediaInfo.HARD_CHINESE
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


def meets_requirements(f: MediaFile) -> tuple[bool, str]:
    """这一份**真的**符合偏好要求吗？返回 (结论, 理由)。

    抓取时的挑选只看发布标题（`preferences.evaluate`）——下载前那是唯一
    可得的信息。下载完之后事实就在文件里了，必须再核实一次：名字写着
    「简繁内封」而容器里一条字幕轨都没有的情况真实发生过
    （2026-09-05 尼古喵喵 S01E08，ABEMA 转载版 711MB 零字幕轨）。

    **内封探得到，内嵌探不到。** 硬字幕烧在画面里，容器里看不见轨道，
    所以「零字幕轨」不等于「没字幕」：此时退回名字证据判断，
    不能因为探不到就判它不合格。
    """
    title = f.torrent_name or f.filename
    info = probe(f.path)

    # **先看文件，再看名字。** 反过来写会把 LoliHouse 整个组判死：它的种子内部
    # 名只写 `[WebRip 1080p HEVC-10bit AAC ASSx2]`，「简繁内封字幕」只出现在
    # Mikan 的站点标题里，硬门槛的关键词一个都不命中——而文件里实实在在躺着
    # 两条中文字幕轨。2026-09-18 初版就是这么写的，36 个钉子里误判了 12 个。
    if info is not None and info.has_chinese:
        return True, "已探到中文内封字幕轨（%d 条）" % info.sub_count

    # 探不到中文轨，才轮到发布名说话。
    v = preferences.evaluate(title)
    if not v.acceptable:
        return False, v.why() + (
            "，文件里也没有中文内封轨" if info is not None else "（文件探测不可用）")
    if info is None:
        return True, v.why() + "（探测不可用，仅凭发布名）"

    # 名字说有中文字幕但容器里没有 —— 多半是内嵌硬字幕，烧在画面里看不见轨道。
    q = parse_quality(title, f.size)
    if "内嵌" in title or (info.sub_count == 0 and (q.simplified or q.traditional)):
        return True, v.why() + "，无内封轨但名字标中文，按内嵌硬字幕计"
    return False, "发布名标了中文字幕，文件里却有 %d 条轨且都不是中文" % info.sub_count


def _prefer_score(f: MediaFile) -> tuple:
    """同一集的多个候选之间，按用户偏好排出该留哪个。分数越高越该留。

    **文件名优先于种子名。** 合并发布的种子（一个种子装 TV 版 + 无删减版）
    里，两个文件共用同一个 `torrent_name`，差别只写在文件名里；拿种子名
    打分会并列，于是"留哪个"变成随机。

    用 `score_only` 而不是 `evaluate`：硬门槛（必须有中文字幕）是整个发布
    的属性，写在种子标题里，单个文件名通常不含那些关键词，走 `evaluate`
    会双双卡在硬门槛上并列 0 分。硬门槛由 `meets_requirements` 单独把关。
    """
    own = preferences.score_only(f.filename)
    joint = preferences.score_only(f.torrent_name or f.filename)
    return (own, joint, f.size)


def _release_agrees(f: MediaFile, show: Show, season: int, ep: int) -> bool:
    """种子的**发布名**是否也认这个集位。

    发布名是 AutoBangumi 改不动的那一个——`torrents/renameFile` 只改文件名，
    显示名原封不动。所以当 AB 已经把文件改成某个 SxxExx、而那个编号是错的
    时候，只看文件名就会把无关的一集算进这个桶：2026-08-31 正是这样把
    2016 年真正的 S01E08 当成重复清进了隔离区（AB 把 `3rd Season - 08`
    改成了 `S01E08`）。

    「封存集位、当轮清理其余」是一条绕过所有权让位的快车道，走这条道之前
    必须让发布名独立确认一次。确认不了就走原来的慢车道，不急这一轮。
    """
    raw = (f.torrent_name or "").strip()
    if not raw:
        return False
    if parse_episode(raw)[1] != ep:
        return False
    dec = declared_season(raw)
    if dec is not None and dec != season and str(dec) not in _season_offsets(show):
        return False
    return True


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

    ep = _apply_offset(raw, ep, show)
    if ep is None:
        return None                      # 换算出非正数：原始集号的口径不对，交给人
    return target, ep


def _titles(show: Show) -> list[str]:
    """这部番可能出现在文件名里的标题：规范标题、目录名、TMDB 标题、AB 的两个标题。"""
    b = show.bangumi or {}
    return [t for t in (show.official_title, show.dir_name, show.tmdb_title,
                        b.get("official_title"), b.get("title_raw")) if t]


def _is_extra(f: MediaFile, show: Show) -> bool:
    """特典 / 菜单 / PV 等周边：只看作品标题**之后**的那部分名字（`naming.is_extra_of`）。
    所有检测器共用这一个判据——特典规则认定不是特典的，改名与判重也要把它当正片。"""
    return is_extra_of(f.filename, _titles(show))


def _is_video(f: MediaFile) -> bool:
    return f.ext in VIDEO_EXTS or (f.ext == ".!qB" and Path(f.path.stem).suffix.lower() in VIDEO_EXTS)


def is_phantom(f: MediaFile) -> bool:
    """种子声明着、盘上却没有（连 `.!qB` 都没有）的文件。

    scan 的来源 1 按 `torrents/files` 出条目，盘上不在的也出（下载中的文件本来就
    可能还没落盘）。判重只收已下完的，所以进了判重桶的"盘上没有"就是幻影：
    LAT-01 那次把有种子的文件当本地文件隔离后，种子 d08f05a7 留下的就是这种；
    用户经 Jellyfin 删文件、手工挪文件也会造出来。
    """
    return (bool(f.torrent_hash) and not os.path.lexists(f.path)
            and not os.path.lexists(str(f.path) + ".!qB"))


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
                if _is_extra(f, show):
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
                if not _is_video(f) or f.is_incomplete or _is_extra(f, show):
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
                # 集位封存：这一集是 media-agent 自己按 preferences 挑的，
                # 而且复核过确实符合要求 —— 那它就是定论，不再拿画质/体积
                # 跟后来的候选比。
                #
                # 不封存会怎样：2026-09-11 抓取按 `+150 +无删减/简中` 选了
                # 尼古喵喵 S01E10 的合并发布（TV 版 + 邪龙解放版同一个种子），
                # 判重随后按画质规则删掉了其中的邪龙解放版、留下 TV 版。
                # 择源想要的东西（无删减、特定字幕组）画质规则根本表达不了，
                # 让它去覆盖择源的结论，等于择源白做。
                # 候选之间**按用户偏好排序再挑**，不能取"第一个通过复核的"。
                # 2026-09-18 EP11 的合并发布种子里装着 TV 版和邪龙解放版两个
                # 文件，共用同一个 torrent_name、复核结果完全一样，取第一个
                # 就是随机——实测选中了 TV 版，正好是用户不要的那份。
                #
                # 幻影（种子说已下完、盘上没有）既不能封存也不能当赢家：探测不到文件时
                # 复核只看发布名，钉了 `ma:` 的幻影会被封存；排序时它的声明大小和
                # 发布名照样算分，比真文件"好"就赢——真文件被当输家移进隔离区，
                # 这一集从库里消失、幻影永远留着（2026-09-26 审查复现，LAT-01 余波）。
                phantoms = {id(f) for f in files if is_phantom(f)}
                if len(phantoms) == len(files):
                    yield Finding(
                        rule=self.id, kind="phantom_only", severity="minor",
                        classified=True,
                        summary=(f"S{season:02d}E{ep:02d} 的 {len(files)} 个候选都是种子声明了、"
                                 f"盘上却没有的幻影，没有可保留的真文件，本轮不做取舍"),
                        show=show.dir_name, path=str(files[0].path),
                        evidence={"files": [f.filename for f in files],
                                  "torrents": [f.torrent_hash for f in files]},
                    )
                    continue
                #
                # **封存要稳定，也不替择源随手二选一**（删除关口 I4 的检测器一侧）：
                # - 复核通过的封存候选来自**不止一个种子**（停滞 48 小时放行换源后、或手动加了
                #   同钉子的种子）：以前只封存偏好分最高的、其余当输家删掉。两个都是择源的
                #   结论，删哪个由人定——报 `seal_conflict`，封存候选一个都不删；偏好分最高的
                #   那个仍当保留方，没封存的照常判输。
                # - 钉着 `ma:`、复核没过，而**探测不可用**（`probe` 为 None：超时、出错）：只看
                #   名字的结论不算数（LoliHouse 的 `ASSx2`，`tests/test_seal_slot.py` 第 3 组），
                #   昨天封存的今天就失封被删。不知道 = 当作封存：它既不当输家、也不当赢家，
                #   报 `seal_unknown` 给人看。探得到而且确实不合格的，照旧 `seal_failed`。
                seals: list = []
                protected: set[int] = set()
                for f in sorted((f for f in files
                                 if _pinned(f) == (season, ep) and id(f) not in phantoms),
                                key=_prefer_score, reverse=True):
                    ok, why = meets_requirements(f)
                    if ok:
                        seals.append((f, why))
                        continue
                    if probe(f.path) is None:
                        protected.add(id(f))
                        yield Finding(
                            rule=self.id, kind="seal_unknown", severity="minor",
                            classified=True,
                            summary=(f"S{season:02d}E{ep:02d} 抓来的这份探测不可用（{why}），"
                                     f"封存与否不可知：本轮既不删它、也不拿它当保留方"),
                            show=show.dir_name, path=str(f.path),
                            torrent_hash=f.torrent_hash,
                            evidence={"file": f.filename, "torrent_name": f.torrent_name,
                                      "reason": why},
                        )
                        continue
                    yield Finding(
                        rule=self.id, kind="seal_failed", severity="important",
                        classified=True,
                        summary=(f"S{season:02d}E{ep:02d} 抓来的这份没通过复核"
                                 f"（{why}），集位不封存，交回画质规则取舍"),
                        show=show.dir_name, path=str(f.path),
                        torrent_hash=f.torrent_hash,
                        evidence={"file": f.filename, "torrent_name": f.torrent_name,
                                  "reason": why},
                    )
                sealed = seals[0] if seals else None
                seal_torrents = sorted({f.torrent_hash for f, _ in seals})
                if len(seal_torrents) > 1:
                    top = sealed[0].torrent_hash
                    protected |= {id(f) for f, _ in seals if f.torrent_hash != top}
                    yield Finding(
                        rule=self.id, kind="seal_conflict", severity="important",
                        classified=True,
                        summary=(f"S{season:02d}E{ep:02d} 有 {len(seal_torrents)} 个不同的种子都钉着"
                                 f"这一集且复核通过，封存不替择源二选一：都不删，需人工挑一个"),
                        show=show.dir_name, path=str(sealed[0].path),
                        evidence={"files": [f.filename for f, _ in seals],
                                  "torrents": seal_torrents,
                                  "reasons": [why for _, why in seals]},
                    )

                pending = [f for f in files if f.torrent_category == "Bangumi"]
                if pending and not sealed:
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

                if sealed:
                    keeper, seal_why = sealed
                    losers = [f for f in files if f is not keeper and id(f) not in protected]
                    # 封存能绕过所有权让位，但绕不过集号可信度。还挂在
                    # `Bangumi` 下的文件，此刻叫什么只是"AB 认为的"；
                    # 要在本轮就清理它，得让它的**发布名**独立确认集位。
                    holdback = [f for f in losers
                                if f.torrent_category == "Bangumi"
                                and not _release_agrees(f, show, season, ep)]
                    if holdback:
                        yield Finding(
                            rule=self.id, kind="pending_ownership", severity="minor",
                            classified=True,
                            summary=(f"S{season:02d}E{ep:02d} 已封存 {keeper.filename}，"
                                     f"但另 {len(holdback)} 个候选的发布名认不出这个集位，"
                                     f"本轮不清理它们"),
                            show=show.dir_name, path=str(keeper.path),
                            evidence={"keep": keeper.filename, "seal": seal_why,
                                      "holdback": [f.torrent_name or f.filename
                                                   for f in holdback]},
                        )
                        losers = [f for f in losers if f not in holdback]
                    reason = "集位已封存：%s" % seal_why
                else:
                    # 真文件永远排在幻影前面（见上文 phantoms 的注释）；封存不可知的既不当
                    # 赢家也不当输家
                    ranked = sorted((f for f in files if id(f) not in protected),
                                    key=lambda f: (id(f) not in phantoms, _rank_for_keep(f)),
                                    reverse=True)
                    if not ranked:
                        continue
                    keeper, losers = ranked[0], ranked[1:]
                    reason = ""

                # 同一个种子里的兄弟文件**只作废这一个文件，不动种子**。
                # 合并发布（一个种子装 TV 版 + 无删减版）里两份都是这个种子的
                # 组成部分：按整种子作废会连保留的那份一起摘掉，做种中断。
                # `file_only` 把它设为不下载再移进隔离区，种子继续为保留的那份
                # 做种——这类发布在两个文件之间垫了 padding 文件，分片不跨文件，
                # 摘掉一个不影响另一个的校验。
                #
                # 用户口径是「只保留」（NUKITASHI 只留青蓝岛版、尼古喵喵只留
                # 邪竜解放版），所以不是挪进隐藏目录留着，而是进隔离区，
                # 保留期内仍可恢复。
                siblings = [l for l in losers
                            if l.torrent_hash and l.torrent_hash == keeper.torrent_hash]
                losers = [l for l in losers if l not in siblings]
                kd = content_digest(keeper.path, cache)
                # 删除关口复核用的保留方与集位（`media_agent/gate.py` 的 I1 / I4）：执行那一刻
                # 保留方还在不在、下完没有、是不是还归这一集，只有点名了才查得了。以前只给
                # `{path, torrent_hash}`，`_op_trash` 从不看保留方——保留方是幻影、被截断、
                # 或同批先被删掉时，输家照删，这一集就从库里消失了。
                keep = {"slot": [season, ep], "keep_path": str(keeper.path),
                        "keep_hash": keeper.torrent_hash, "keep_size": keeper.size,
                        "keep_digest": kd}
                for sib in siblings:
                    yield Finding(
                        rule=self.id, kind="bundled_version", severity="important",
                        summary=(f"S{season:02d}E{ep:02d} 同一个种子里还装着 "
                                 f"{sib.filename}，与保留的 {keeper.filename} 同集；"
                                 f"只作废这一个文件，种子继续做种"),
                        show=show.dir_name, path=str(sib.path),
                        torrent_hash=sib.torrent_hash,
                        evidence={"keep": keeper.filename, "torrent": sib.torrent_name,
                                  "keep_score": _prefer_score(keeper)[0],
                                  "drop_score": _prefer_score(sib)[0]},
                        action=Action(op="trash", reversible=True,
                                      args={"path": str(sib.path),
                                            "torrent_hash": sib.torrent_hash,
                                            "file_only": True, **keep,
                                            **({"phantom": True} if id(sib) in phantoms
                                               else {})},
                                      note="合并发布的另一版本：设为不下载并移入隔离区"),
                    )

                for loser in losers:
                    ld = content_digest(loser.path, cache)
                    identical = bool(kd and ld and kd == ld)
                    # 幻影输家：盘上没有可搬的文件，要处置的是那条种子记录——它不摘，
                    # 赢家改到集位名上就成了"两个种子宣称同一路径"。执行器凭这个标记、
                    # 且执行时复核仍是幻影，才摘记录（可凭 magnet 回退）。
                    phantom = id(loser) in phantoms
                    yield Finding(
                        rule=self.id, kind=self.kind, severity="important",
                        summary=(f"S{season:02d}E{ep:02d} 重复："
                                 f"{'集位已封存，' if sealed else ''}"
                                 f"保留 {keeper.filename}，清理 {loser.filename}"
                                 f"{'（幻影：种子说已下完、盘上没有）' if phantom else ''}"),
                        show=show.dir_name, path=str(loser.path),
                        torrent_hash=loser.torrent_hash,
                        evidence={
                            "keep": str(keeper.path), "keep_size": keeper.size,
                            "drop_size": loser.size,
                            "keep_digest": kd, "drop_digest": ld,
                            "byte_identical": identical, "phantom": phantom,
                            "reason": ("字节完全相同" if identical
                                       else reason or "同集不同版本，按画质取舍"),
                        },
                        # 输家一律 `file_only`：它若在多文件合集里（3年Z组银八老师
                        # [01-12]），整种子作废会让其余集跟着失去做种、回退加不回来——
                        # 生产审计 63 条 duplicate-episode 的整种子作废就是这么来的。
                        # 种子只有这一个文件时，执行器自己退回整种子作废（删除关口 I3）。
                        action=Action(op="trash", reversible=True,
                                      args={"path": str(loser.path),
                                            "torrent_hash": loser.torrent_hash,
                                            **({"file_only": True} if loser.torrent_hash
                                               else {}),
                                            **keep,
                                            **({"phantom": True} if phantom else {})},
                                      note=("幻影：只摘种子记录（可凭 magnet 回退）" if phantom
                                            else "移入隔离区，保留期内可恢复")),
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
                if not _is_video(f) or _is_extra(f, show):
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
def is_dead_now(t: dict) -> bool:
    """此刻的**瞬时**死亡特征：没下完、没人做种、全网拼不出完整副本。

    检测器与执行器（`drop_torrent` 执行前的活体复核）共用这一个判据。
    """
    return (t.get("progress", 0) < 1.0
            and t.get("state") in ("stalledDL", "downloading", "metaDL")
            and (t.get("num_complete") or 0) <= 0
            and (t.get("num_seeds") or 0) <= 0
            and (t.get("availability") or 0) <= 0)


def last_sign_of_life(t: dict, now: float) -> float:
    """最后一次"活着"的时刻：加入、收发数据、见到完整副本，三者取最晚。

    以前只看 `added_on`——量的是**加入多久**，不是**停滞多久**：半年前加的、
    一小时前还在收数据的种子也会被判死；`relink_torrent` 触发 recheck 后
    卡在 99.8% 的老种子同样一上来就"停滞了几个月"。

    `last_activity`（最后一次收发数据）/ `seen_complete`（最后一次见到完整副本）
    按 WebAPI 文档是 Unix 时间戳；**没有逐条在生产上实测**。所以取 max：
    字段缺失、为 0/-1、或在未来，都自动退回 `added_on`，不会比以前更激进。
    """
    stamps = [t.get("added_on") or 0, t.get("last_activity") or 0,
              t.get("seen_complete") or 0]
    return max([s for s in stamps if 0 < s <= now + 60] or [now])


def _library_show_of(save_path: str, media_root: Path) -> str | None:
    """种子落在哪部番的目录里；不在媒体库的番剧目录里返回 None。

    与 scan 同一口径：以 `.` 开头的一级目录（`.staging` 等手动暂存区）不算。
    """
    sp = (save_path or "").rstrip("/")
    root = str(media_root).rstrip("/")
    if not sp.startswith(root + "/"):
        return None
    top = sp[len(root) + 1:].split("/", 1)[0]
    return None if not top or top.startswith(".") else top


def _entries_or_none(ctx: Context, torrent_hash: str) -> list[dict] | None:
    """种子的文件列表；读不到返回 None（= 不知道）。"""
    try:
        return ctx.qbit.files(torrent_hash) if ctx.qbit else None
    except Exception:
        return None


def completed_members(ctx: Context, torrent_hash: str) -> list[str] | None:
    """种子里已经下完、仍要下载的成员文件；读不到文件列表返回 None（= 不知道）。"""
    entries = _entries_or_none(ctx, torrent_hash)
    if entries is None:
        return None
    return [e["name"] for e in entries
            if e.get("priority", 1) != 0 and e.get("progress", 0) >= 1]


def droppable_dead(ctx: Context, t: dict, now: float) -> bool:
    """这一轮 dead-torrent 会不会**摘掉**它——与检测器完全同一个判据。

    抓取的换源放行（`grab._inflight`）必须用它：放行抓新源（op 0）的那一轮，
    旧种子就得同批被摘掉（op 1），否则新种子被改到同一个集位名上，两个种子抢
    一个文件。以前放行看的是 `now - added_on`，而死种从 2026-09-26 起改按
    "最后一次活着"计时——72 小时前加入、10 小时前还在收数据的种子被放行换源、
    却不算死，新旧并存（审查复现）。有已下完成员的死种检测器只报告不摘，这里
    同样不放行；读不到文件列表（`completed_members` 为 None）也不放行。
    """
    if not is_dead_now(t):
        return False
    if _library_show_of(t.get("save_path", ""), ctx.config.media_root) is None:
        return False
    if now - last_sign_of_life(t, now) < ctx.config.dead_torrent_hours * 3600:
        return False
    return completed_members(ctx, t.get("hash", "")) == []


class DeadTorrentDetector:
    """0 做种 + availability 0 + 长期停滞 = 死种，等下去也不会有进度。

    出处：《异世界四重奏》S02 整季 —— 所有 tracker 都报 seeds=0，
    availability=0 表示全网 peer 拼不出一份完整文件，换源也无解。

    **处置只针对种子记录本身，磁盘一个字节都不动**（testinfra B1）。
    早先的动作是 `trash{path: content_path}`，而 NoSubfolder 多文件种子
    （本项目自己抓的种子默认就是这个布局，生产上 10 个）的 content_path 就是
    整个 `Season N` 目录——一个死种会把整季、别的种子的文件、所有封存集位一起
    搬进隔离区，目录的 `st_size` 还让体积配额形同虚设。同一目录下两个死种的
    finding 又因 key 相同塌成一条（critic N7），所以这里按 hash 出 finding。
    实际上生产 3 次死种处置都是 `freed 0`：content_path 不带 `.!qB`，
    从来只摘了记录——现在把这一点明确下来。

    另外两道收窄：
    - **只管媒体库番剧目录里的种子**：2026-09-08 它删了 `Media/.staging/opm-oad/`
      下三个手动暂存的种子。
    - **有已下完的成员文件就只报告**：那是可播的正片、还在做种，"死"不等于"没用"。
    """
    id = "dead-torrent"
    kind = "dead_torrent"

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        import time
        threshold = ctx.config.dead_torrent_hours * 3600
        now = time.time()
        for t in state.torrents:
            if not is_dead_now(t):
                continue
            show = _library_show_of(t.get("save_path", ""), ctx.config.media_root)
            if show is None:
                continue
            stalled = now - last_sign_of_life(t, now)
            if stalled < threshold:
                continue
            h = t.get("hash", "")
            entries = _entries_or_none(ctx, h)
            if entries is None:
                continue                  # 看不到文件列表就不下结论
            done = [e["name"] for e in entries
                    if e.get("priority", 1) != 0 and e.get("progress", 0) >= 1]
            evidence = {"num_seeds": t.get("num_seeds"),
                        "num_complete": t.get("num_complete"),
                        "availability": t.get("availability"),
                        "progress": t.get("progress"),
                        "stalled_hours": round(stalled / 3600),
                        "content_path": t.get("content_path", "")}
            name = t.get("name", "")[:60]
            if done:
                yield Finding(
                    rule=self.id, kind=self.kind, severity="important",
                    summary=(f"死种里有 {len(done)} 个已下完的文件，自动摘种会让它们"
                             f"失去做种，需人工决定：{name}"),
                    show=show, torrent_hash=h,
                    evidence={**evidence, "completed_files": done[:5]},
                )
                continue
            yield Finding(
                rule=self.id, kind=self.kind, severity="important",
                summary=f"死种（全网无完整副本，已停滞 {stalled/3600:.0f}h）：{name}",
                show=show, torrent_hash=h, evidence=evidence,
                action=Action(op="drop_torrent", reversible=True,
                              args={"torrent_hash": h, "dead": True,
                                    "name": t.get("name", ""),
                                    "magnet": t.get("magnet_uri", ""),
                                    "save_path": t.get("save_path", ""),
                                    "category": t.get("category", ""),
                                    "tags": t.get("tags", "")},
                              note="只摘种子记录（可凭 magnet 回退），磁盘上的文件一个字节都不动；"
                                   "它自己的 .!qB 半成品另由 dead_partial 处置"),
            )
            yield from self._partials(show, t, entries)

    def _partials(self, show: str, t: dict, entries: list[dict]) -> Iterable[Finding]:
        """死种**自己的**半成品：没下完、优先级非 0 的条目在盘上的 `X.!qB`，逐个移进隔离区。

        第 1 阶段只摘记录、半成品留在盘上：它占着 94% 满的 APFS 容器，更占着集位名——
        停滞换源时新种子下完也改不过去（占用闸门按设计拦下，改过去就是接着往那份半成品里
        写）。执行在摘记录（op 1）之后（op 5）；删除关口要求这个种子**同一批里真的被摘掉了**
        （摘除被跳过——又有了做种——半成品就不动），另一个种子仍声明着 `X` 时不动（I2）。
        """
        sp = Path((t.get("save_path") or "").rstrip("/") or "/")
        h = t.get("hash", "")
        for e in entries:
            if e.get("priority", 1) == 0 or e.get("progress", 0) >= 1:
                continue
            partial = Path(str(sp / e["name"]) + ".!qB")
            if partial.is_symlink() or not partial.is_file():
                continue
            yield Finding(
                rule=self.id, kind="dead_partial", severity="minor",
                summary=f"死种的半成品：{partial.name}",
                show=show, path=str(partial), torrent_hash=h,
                evidence={"entry": e["name"], "entry_progress": e.get("progress"),
                          "torrent": t.get("name", "")[:110]},
                action=Action(op="trash", reversible=True,
                              args={"path": str(partial), "torrent_hash": h},
                              note="死种摘记录之后，它自己的 .!qB 半成品移入隔离区（可回退）"),
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

    **只看标题之后的部分，而且三种不碰**（2026-09-26 修）。以前 `is_extra` 用在整个文件名上，
    规范名是 `{TMDB 标题} SxxEyy.ext`：标题里带 trailer / preview / menu / PV / 特典 / 菜单的番，
    每一集都会被当成特典移进隔离区（与"The Ghost **in** the Shell"同一类）。删除关口对"特典处置、
    没钉 `ma:`"的文件放行，挡不住这一类，所以这里要自己收紧：
    - 钉着 `ma:` 的**正片**（种子里唯一的视频，或名字认得出钉着的集号）：它是抓取器认定的那一集、
      封存着，归判重管。钉着的合集里的 NCOP 不是那一集，照旧清理。
    - 某集**唯一**的可播文件：名字认得出集号、而同一集没有别的下完了的正片——宁可留着一个真特典，
      也不删掉唯一的一集（标题记号藏在认不出的罗马音标题里时靠这一条）。
    """
    id = "extras-in-library"
    kind = "extra_content"

    @staticmethod
    def _is_pinned_episode(f: MediaFile, show: Show) -> bool:
        pin = _pinned(f)
        if not pin:
            return False
        videos = [o for o in show.files if o.torrent_hash == f.torrent_hash and _is_video(o)]
        return len(videos) <= 1 or parse_episode(f.filename)[1] == pin[1]

    @staticmethod
    def _only_copy(f: MediaFile, show: Show) -> bool:
        slot = _resolve(f, show)
        if not slot:
            return False
        return not any(o is not f and _is_video(o) and not o.is_incomplete
                       and not _is_extra(o, show) and _resolve(o, show) == slot
                       for o in show.files)

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        for show in state.shows:
            for f in show.files:
                if not _is_extra(f, show):
                    continue
                if not _is_video(f):
                    continue
                if self._is_pinned_episode(f, show):
                    continue
                if self._only_copy(f, show):
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
