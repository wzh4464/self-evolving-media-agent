"""隔离区清理：只删「绝对没问题」的那些。

隔离区是删除的唯一形态——`trash` 把文件移进来并记下逆操作，30 天后才算真正
可以消失。但保留期一到就无差别清空是危险的：万一当初判重判错了，
或者留下的那份其实是个截断的空壳，隔离区就是最后一份原件。

所以这里不按时间清，按**可验证的安全条件**清。一份隔离文件进入待删池，
必须同时满足：

1. **库里有替代者。** 从它当初的路径推出集位（剧集目录 / 季 / 集号），
   看库里现在有没有文件占着这个集位。注意是查**当前实际状态**，
   不是信审计日志里记的"保留了谁"——日志是历史，文件可能后来又被换过。

2. **替代者是完整的。** 有两条互斥的证明路径，满足其一即可：

   a. **种子校验**（最强）：它有对应的种子，磁盘大小**恰好等于**
      `torrents/files` 里种子声明的大小，且种子进度 100%。

   b. **时长自证**（替代者没有种子时）：BD 合集、手工导入、种子早已删除的
      文件都没有种子可查，但"完整"本身是可以独立验证的——
      时长落在同季其他集的中位数附近，且**尾部能真正解出画面**。
      截断的半成品必然在这两条上露馅：要么时长明显偏短，
      要么容器头写着完整时长而尾部根本解不出帧。

   光有文件占位不够——一个下了一半的文件同样占着集位，
   拿它当"库里已经有了"的证据，就会把唯一完整的那份删掉。

任何一条不满足就留在隔离区。宁可占着 54 GB，不可删错一份。

**天然直接放行的：**

- `.!qB` 残片：qBittorrent 给未下完的文件加的后缀。按定义它就不是一份
  可用的拷贝，不存在"万一它是最后一份原件"的风险，不必找替代者。
  **前提是没有种子还指着它**——还有种子占用该路径就说明这是正在下载
  （或暂停中）的活数据，删了会丢掉已下的分片、逼它从头重下。

**哪些天然进不了池：**

- `extras-in-library` 清掉的特典/PV：它们被清理正是因为 TMDB 里没有对应条目，
  按定义就不存在"库里的替代者"。它们的去留是另一种判断，不在本机制内。
- 既没有审计记录、也没有 `purge.jsonl` 记录，且路径 + TMDB 也反推不出集位的：
  无从得知它当初代表哪一集，验证不了。

**身份来源有两个日志，都要读。** `audit.jsonl` 记的是 agent 自动跑出来的
`trash` 动作；`purge.jsonl` 记的是人（或 agent 在会话中）手工移入隔离区的动作
（版本替换、错误恢复的撤销……）。只读前者会把后者的产物全判成"没有审计记录"，
明明有据可查却卡在池外。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .naming import VIDEO_EXTS, parse_episode, season_of_dir
from .probe import duration as _duration, tail_decodes as _tail_decodes

_SXXEXX = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)


@dataclass
class Candidate:
    """隔离区里的一份文件，及其能否安全删除的判定。"""
    trash_path: Path
    size: int
    origin: str = ""          # 当初在库里的路径
    rule: str = ""            # 当初被哪条规则清理
    slot: tuple | None = None  # (季, 集)
    survivor: Path | None = None
    eligible: bool = False
    why: str = ""


def _audit_by_trash_path(audit_log: Path) -> dict:
    """`trashed_to` -> 审计记录。同一路径若被多次记录，以最后一条为准。"""
    out: dict[str, dict] = {}
    if not audit_log.exists():
        return out
    for line in audit_log.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("op") == "trash" and r.get("status") == "applied" and r.get("trashed_to"):
            out[r["trashed_to"]] = r
    return out


def _manual_by_trash_path(purge_log: Path, trash_root: Path) -> dict:
    """`purge.jsonl` 里手工移入隔离区的记录：目标路径 -> 记录。

    `audit.jsonl` 只有 agent 自动跑出来的 `trash`。人在会话里做的移动
    （换版本、撤销错误的恢复）写在 `purge.jsonl`，形如
    `{"op": "version_swap", "from": 库内路径, "to": 隔离路径, "basis": ...}`。
    这些同样是有据可查的身份证明，不认它就等于把自己的手工操作当成来历不明。

    只收 `to` 落在隔离区内的记录——`purge.jsonl` 里也有反方向的
    （`restore_from_trash` 把文件从隔离区捞回库里），那些不是这里要的。
    """
    out: dict[str, dict] = {}
    if not purge_log.exists():
        return out
    root = str(trash_root).rstrip("/") + "/"
    for line in purge_log.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        to = r.get("to") or ""
        if to.startswith(root) and r.get("from"):
            out[to] = r
    return out


def _slot_of(name: str, summary: str, season_hint: int | None = None) -> tuple | None:
    """从文件名取 (季, 集)；取不到就退回摘要里的 `S03E09 重复：…`。

    集号解析走 `naming.parse_episode`，不要自己写正则——发布组的写法远不止
    `SxxExx` 一种，`[06]`、`- 06 [1080p]`、`第06话` 都很常见。原先这里
    只认 `S(\\d+)E(\\d+)`，于是攻壳机动队的 `... - The Ghost in the Shell [06]`
    被判成"解析不出集号"，明明路径里就写着剧名。

    `parse_episode` 可能只给出集号、给不出季号（`[06]` 这种形式本来就没有季）。
    这时用 `season_hint`——调用方从库里数出该剧只有一个季目录，那就是它。
    数不出唯一答案就返回 None：**猜季号会让文件对到错误的集位上**。
    """
    for s in (name, summary):
        if not s:
            continue
        sn, ep = parse_episode(s)
        if ep is None:
            continue
        if sn is None:
            sn = season_hint
        if sn is None:
            continue
        return sn, ep
    return None


def _sole_season(show_dir: Path) -> int | None:
    """该剧在库里只有一个 `Season N` 目录时返回 N，否则 None。"""
    seasons = []
    for d in show_dir.iterdir():
        sn = season_of_dir(d.name) if d.is_dir() else None
        if sn is not None:
            seasons.append(sn)
    return seasons[0] if len(seasons) == 1 else None


def _torrent_size_index(qbit) -> dict:
    """磁盘绝对路径 -> (种子声明大小, 种子进度)。"""
    idx: dict[str, tuple] = {}
    if not qbit:
        return idx
    for t in qbit.torrents():
        sp = (t.get("save_path") or "").rstrip("/")
        if not sp:
            continue
        for f in qbit.files(t["hash"]):
            if f.get("priority", 1) == 0:
                continue
            idx[str(Path(sp) / f["name"])] = (f.get("size", 0), t.get("progress", 0.0))
    return idx


def _season_median_duration(show_dir: Path, sn: int, exclude: Path,
                            cache: dict) -> float | None:
    """同一季其他集时长的中位数；不足 2 集参照就返回 None。

    这是"替代者没有种子"时唯一还站得住的完整性标尺：同一部番同一季的正片
    时长高度一致，截断的那份会明显偏短。参照必须**排除替代者自己**，
    否则它自己会把中位数拉过去。
    """
    key = (str(show_dir), sn)
    if key not in cache:
        ds = []
        for sub in show_dir.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            for q in sub.iterdir():
                if q.suffix.lower() not in VIDEO_EXTS or q.name.startswith("._"):
                    continue
                m = _SXXEXX.search(q.name)
                if not m or int(m.group(1)) != sn:
                    continue
                d = _duration(q)
                if d:
                    ds.append((str(q), d))
        cache[key] = ds
    ds = [d for p, d in cache[key] if p != str(exclude)]
    if len(ds) < 2:
        return None
    ds.sort()
    n = len(ds)
    return ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) / 2


def _identify_from_path(trash_file: Path, media_root: Path, tmdb, cfg):
    """无审计记录时，从隔离路径 + TMDB 反推 (库内原路径, (季, 集))。

    返回 None 表示推不出来——推不出来就留着，绝不猜。
    """
    shows = {d.name: d for d in media_root.iterdir()
             if d.is_dir() and not d.name.startswith(".")}

    # 剧名有两个来源，路径**和**文件名都要看。
    #
    # 原先只拿路径的每一段去比对库内目录名。但手工整理留下的批次目录形如
    # `manual-20260830T130637/Season 1/…`——整条路径里根本没有剧名段，
    # 于是 13 份 `3年Z组银八老师 S01E07.mp4` 全被判成"来历不明"，
    # 而剧名明明白白写在文件名开头。
    #
    # 文件名匹配取**最长**的那个库内剧名，避免 `入间同学入魔了！` 抢走
    # 本该属于 `入间同学入魔了！！！` 的文件。
    show_dir = next((shows[part] for part in trash_file.parts if part in shows), None)
    if show_dir is None:
        stem = trash_file.stem
        cands = [s for s in shows if stem.startswith(s)]
        if cands:
            show_dir = shows[max(cands, key=len)]
    if show_dir is None:
        return None

    slot = _slot_of(trash_file.name, "", season_hint=_sole_season(show_dir))
    if not slot:
        return None
    if tmdb is None:
        return None
    from . import sidecar as sc_mod
    try:
        tid = sc_mod.load(show_dir).tmdb_id
        if not tid:
            return None
        eps = {e["episode_number"] for e in tmdb.season_episodes(tid, slot[0])}
    except Exception:
        return None
    if slot[1] not in eps:
        return None                      # TMDB 里没这一集，身份存疑
    return str(show_dir / ("Season %d" % slot[0]) / trash_file.name), slot


def build_pool(cfg, qbit, tmdb=None) -> list[Candidate]:
    """扫描隔离区，逐份判定能否安全删除。"""
    trash_root = Path(cfg.trash_dir)
    media_root = Path(cfg.media_root)
    audit = _audit_by_trash_path(Path(cfg.audit_log))
    manual = _manual_by_trash_path(Path(cfg.state_dir) / "purge.jsonl", trash_root)
    sizes = _torrent_size_index(qbit)
    dur_cache: dict = {}

    out: list[Candidate] = []
    for p in sorted(trash_root.rglob("*")):
        if not p.is_file() or p.name.startswith("._"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        c = Candidate(trash_path=p, size=size)

        # `.!qB` 是 qBittorrent 给没下完的文件加的后缀。它按定义不是一份可用
        # 拷贝，"万一它是最后一份原件"这个顾虑对它不成立，所以不必找替代者。
        #
        # 但**不能无条件放行**：还有种子指着它，就说明这是一份正在下载
        # （或暂停中）的活数据，删掉会毁掉已下的分片、逼它从头再来。
        # 必须先确认 qBittorrent 里没有任何种子占用这个路径。
        # 种子索引存的是干净路径，所以去掉后缀再比对。
        if p.name.endswith(".!qB"):
            clean = str(p)[: -len(".!qB")]
            if str(p) in sizes or clean in sizes:
                c.why = "未完成分片，但仍有种子在下载它，删了会丢已下分片"
            else:
                c.eligible = True
                c.why = ("qBittorrent 未完成分片（`.!qB`），本身不是可用拷贝；"
                         "且已核对无任何种子占用该路径")
            out.append(c)
            continue

        # 目录元数据不是剧集，别拿"是哪一集"去问它。
        #
        # `tvshow.nfo` / `.media-agent.json` 是**派生文件**：前者由 nfo 规则
        # 生成，后者由 sidecar-sync 生成，两者都能随时重建。它们会进隔离区
        # 是因为所属目录被合并/改名掉了（`上伊那牡丹，醉姿如百合，醉姿如百合`
        # 这种标题漂移留下的空壳）。原先它们混在"无法反推是哪一集"里，
        # 让那个分类看起来有 23 份、实际只有 15 份是真的识别失败。
        if p.name in ("tvshow.nfo", ".media-agent.json") or p.suffix.lower() == ".nfo":
            c.eligible = True
            c.rule = "（目录元数据）"
            c.why = ("目录元数据（%s），由 nfo / sidecar-sync 规则生成，"
                     "所属目录已不存在，需要时可重建" % p.name)
            out.append(c)
            continue

        rec = audit.get(str(p))
        man = None if rec else manual.get(str(p))
        if man:
            # 手工移入的：`purge.jsonl` 里记着它从哪来、为什么移，
            # 身份和自动 trash 一样确凿，走同一套替代者校验。
            c.rule = "（%s，purge.jsonl）" % (man.get("op") or "手工")
            c.origin = man.get("from") or ""
            c.slot = _slot_of(Path(c.origin).name, p.name)
            if not c.slot:
                c.why = "purge.jsonl 有记录，但原路径里解析不出集号"
                out.append(c)
                continue
            rec = None                   # 后续仍按"无 audit 记录"走时长复核
        elif not rec:
            # 没有审计记录时，改用**结果名单**反推身份：
            #
            # 文件名里的 SxxExx 加上路径里能对上的剧集目录，就唯一确定了一集；
            # 再由 TMDB 确认"这部番确实有这一集"。TMDB 是这套系统里集数的
            # 权威来源——它说该有 S01E01，那么隔离区里叫 S01E01 的那份
            # 就只能对应它，不需要审计日志来告诉我们。
            #
            # 路径位置不可靠（批次目录形状不一，有 `2026-08-19/上伊那牡丹-残留目录/…`
            # 这种把标签放在剧名位置的），所以是拿**每一段**去和库内目录名精确比对，
            # 而不是取固定下标。
            ident = _identify_from_path(p, media_root, tmdb, cfg)
            if not ident:
                c.why = "没有审计记录，且无法从路径与 TMDB 反推是哪一集"
                out.append(c)
                continue
            c.rule, c.origin, c.slot = "（由路径+TMDB 反推）", ident[0], ident[1]
        else:
            c.rule = rec.get("rule") or ""
            c.origin = (rec.get("args") or {}).get("path") or ""
        if rec and c.rule != "duplicate-episode":
            # 措辞不能写成"按定义不存在库内替代者"——2026-09-04 核对发现那是错的。
            # `extras-in-library` 清掉的 120 份里，84 份确实是碟片菜单和无字幕
            # OP/ED（TMDB 不收录），但另外 36 份是 BD 特典，而 TMDB 的 Season 0
            # **有**对应条目——只是特典的文件名（`[Tokuten][01]`、`[SP03] … - 01`）
            # 跟 TMDB 的中文标题（「URA-ON!～唯的好奇心系～」）毫无字面关联，
            # 靠名字或集号都匹配不上，于是被一律判成"TMDB 里没有"。
            # 结论（不删）是对的，理由是错的；错的理由会误导下一个读它的人。
            c.why = (f"清理原因是 {c.rule}；这类是否真在 TMDB 无对应需要逐个核对，"
                     f"不自动删")
            out.append(c)
            continue

        if rec:
            c.slot = _slot_of(Path(c.origin).name, rec.get("summary") or "")
        if not c.slot:
            c.why = "解析不出集号"
            out.append(c)
            continue

        # 集位要在**整部番的各季目录**里找，不能只看原路径的父目录。
        #
        # 两种情况会让"只看父目录"给出错误答案：
        #   - 文件当初在库内的 `.other` / `.extras` 隔离子目录里（那不是集位，
        #     真正的正片在 `Season N/` 下）——实测《义妹生活》S01E07、
        #     《药屋少女的呢喃》S01E24 都因此被误报成"库里是空的，
        #     这份可能是唯一原件"，而它们的正片明明都在。
        #   - 整部番按 TMDB 重编排过（药屋的 Season 2 并进了 Season 1）。
        # 误报比漏报更伤：这道检查一旦开始喊狼来了，就没人再信它。
        show_dir = Path(c.origin)
        while show_dir.parent != media_root and show_dir.parent != show_dir:
            show_dir = show_dir.parent
        if not show_dir.is_dir():
            c.why = "原剧集目录已不存在（可能被移动或改名过）"
            out.append(c)
            continue

        # 条件 1：库里现在有没有文件占着这个集位
        sn, ep = c.slot
        holders = []
        for sub in show_dir.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue        # `.other` / `.extras` 是隔离区，不算库内占位
            for q in sub.iterdir():
                if q.suffix.lower() not in VIDEO_EXTS or q.name.startswith("._"):
                    continue
                m = _SXXEXX.search(q.name)
                if m and (int(m.group(1)), int(m.group(2))) == (sn, ep):
                    holders.append(q)
        if not holders:
            c.why = f"库里 S{sn:02d}E{ep:02d} 现在是空的——这份可能是唯一的原件"
            out.append(c)
            continue
        if len(holders) > 1:
            c.why = (f"库里 S{sn:02d}E{ep:02d} 有 {len(holders)} 个文件，先解决重复再说")
            out.append(c)
            continue

        # 条件 2：占位者必须被证明是完整文件。两条路径，满足其一即可。
        surv = holders[0]
        c.survivor = surv
        want = sizes.get(str(surv))
        if want is not None:
            # 路径 a：种子校验。最强的证据——种子声明多少字节就该有多少字节。
            declared, progress = want
            actual = surv.stat().st_size
            if progress < 1.0:
                c.why = f"替代者的种子只下到 {progress*100:.1f}%"
                out.append(c)
                continue
            if actual != declared:
                c.why = (f"替代者大小与种子声明不符（磁盘 {actual}，种子 {declared}）")
                out.append(c)
                continue
            complete_why = f"大小与种子声明一致（{declared} 字节）且已完成"
        else:
            # 路径 b：时长自证。BD 合集、手工导入、种子早被删掉的文件都没有
            # 种子可查，但"完整"不必非得由种子来证明——
            #   1. 时长落在同季其他集的中位数附近（截断的会明显偏短）；
            #   2. 尾部真能解出画面（防的是容器头写着完整时长、数据其实没写完，
            #      这种只查时长是查不出来的）。
            # 两条都过才算数。任一条取不到证据就留着，不做"没查出问题=没问题"。
            d_surv = _duration(surv)
            if d_surv is None:
                c.why = "库内替代者没有种子，且读不出时长，无法确认完整"
                out.append(c)
                continue
            med = _season_median_duration(show_dir, sn, surv, dur_cache)
            if med is None:
                c.why = (f"库内替代者没有种子，同季也不足 2 集可作时长参照"
                         f"（替代者 {d_surv:.0f}s）")
                out.append(c)
                continue
            if abs(d_surv - med) > max(30.0, 0.02 * med):
                c.why = (f"替代者时长 {d_surv:.0f}s 偏离同季中位数 {med:.0f}s，"
                         f"疑似截断或并非正片")
                out.append(c)
                continue
            if not _tail_decodes(surv, d_surv):
                c.why = (f"替代者时长看着正常（{d_surv:.0f}s）但尾部解不出画面，"
                         f"疑似写入未完成的空壳")
                out.append(c)
                continue
            complete_why = (f"无种子可校验，改以时长自证：{d_surv:.0f}s 与同季中位数 "
                            f"{med:.0f}s 相符，且尾部可解码")

        # 没有审计记录时，"是不是重复"这件事没人判过——只知道集位被占。
        # 万一隔离的那份其实是想留的另一个版本（NUKITASHI 的青蓝岛版那种），
        # 光看集位是发现不了的。所以再用时长确认一次。
        #
        # **这道检查的能力边界要清楚**：同一部番每集时长都差不多
        # （实测攻壳 E01 和 E02 都是 1444 秒），所以时长**分不出是哪一集**——
        # 那由 SxxExx 的集位匹配来定。它挡的是另一类：截断的半成品、
        # 混进来的特典或剧场版、时长明显不同的剪辑版本。
        #
        # 有审计记录的那条路径走的是 duplicate-episode 的判定，不必重做。
        if not rec:
            d1, d2 = _duration(p), _duration(surv)
            if d1 is None or d2 is None:
                c.why = "无审计记录且读不出时长，无法确认两者是同一集内容"
                out.append(c)
                continue
            if abs(d1 - d2) > max(30.0, 0.05 * max(d1, d2)):
                c.why = (f"时长差异过大（隔离 {d1:.0f}s vs 库内 {d2:.0f}s），"
                         f"可能不是同一集内容或是想保留的另一版本")
                out.append(c)
                continue
            c.why = (f"S{sn:02d}E{ep:02d} 由 {surv.name} 占位，{complete_why}；"
                     f"时长 {d1:.0f}s≈{d2:.0f}s 确认同集")
            c.eligible = True
            out.append(c)
            continue

        c.eligible = True
        c.why = f"S{sn:02d}E{ep:02d} 由 {surv.name} 占位，{complete_why}"
        out.append(c)
    return out
