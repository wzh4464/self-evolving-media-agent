"""隔离区清理：只删「绝对没问题」的那些。

隔离区是删除的唯一形态——`trash` 把文件移进来并记下逆操作，30 天后才算真正
可以消失。但保留期一到就无差别清空是危险的：万一当初判重判错了，
或者留下的那份其实是个截断的空壳，隔离区就是最后一份原件。

所以这里不按时间清，按**可验证的安全条件**清。一份隔离文件进入待删池，
必须同时满足：

1. **库里有替代者。** 从它当初的路径推出集位（剧集目录 / 季 / 集号），
   看库里现在有没有文件占着这个集位。注意是查**当前实际状态**，
   不是信审计日志里记的"保留了谁"——日志是历史，文件可能后来又被换过。

2. **替代者是完整的。** 它必须有对应的种子，且磁盘大小**恰好等于**
   `torrents/files` 里种子声明的大小，且种子进度 100%。
   光有文件占位不够——一个下了一半的文件同样占着集位，
   拿它当"库里已经有了"的证据，就会把唯一完整的那份删掉。

任何一条不满足就留在隔离区。宁可占着 54 GB，不可删错一份。

**哪些天然进不了池：**

- `extras-in-library` 清掉的特典/PV：它们被清理正是因为 TMDB 里没有对应条目，
  按定义就不存在"库里的替代者"。它们的去留是另一种判断，不在本机制内。
- 没有审计记录的（早期手工整理留下的 `manual-*` / `dupe-*` 目录）：
  无从得知它当初代表哪一集，验证不了。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .naming import VIDEO_EXTS

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


def _slot_of(name: str, summary: str) -> tuple | None:
    """从文件名取 (季, 集)；取不到就退回摘要里的 `S03E09 重复：…`。"""
    for s in (name, summary):
        m = _SXXEXX.search(s or "")
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


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


def _duration(path: Path) -> float | None:
    """用 ffprobe 取时长（秒）。取不到返回 None。"""
    import subprocess
    for exe in ("/opt/homebrew/bin/ffprobe", "ffprobe"):
        try:
            r = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def _identify_from_path(trash_file: Path, media_root: Path, tmdb, cfg):
    """无审计记录时，从隔离路径 + TMDB 反推 (库内原路径, (季, 集))。

    返回 None 表示推不出来——推不出来就留着，绝不猜。
    """
    slot = _slot_of(trash_file.name, "")
    if not slot:
        return None
    shows = {d.name: d for d in media_root.iterdir()
             if d.is_dir() and not d.name.startswith(".")}
    show_dir = next((shows[part] for part in trash_file.parts if part in shows), None)
    if show_dir is None:
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
    sizes = _torrent_size_index(qbit)

    out: list[Candidate] = []
    for p in sorted(trash_root.rglob("*")):
        if not p.is_file() or p.name.startswith("._"):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        c = Candidate(trash_path=p, size=size)
        rec = audit.get(str(p))
        if not rec:
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
            c.why = f"清理原因是 {c.rule}，按定义不存在库内替代者"
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

        # 条件 2：占位者必须是种子校验过的完整文件
        surv = holders[0]
        c.survivor = surv
        want = sizes.get(str(surv))
        if want is None:
            c.why = "库内替代者没有对应种子，无法核对大小"
            out.append(c)
            continue
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
            c.why = (f"S{sn:02d}E{ep:02d} 由 {surv.name} 占位，大小与种子声明一致"
                     f"（{declared} 字节）且已完成；时长 {d1:.0f}s≈{d2:.0f}s 确认同集")
            c.eligible = True
            out.append(c)
            continue

        c.eligible = True
        c.why = (f"S{sn:02d}E{ep:02d} 由 {surv.name} 占位，"
                 f"大小与种子声明一致（{declared} 字节）且已完成")
        out.append(c)
    return out
