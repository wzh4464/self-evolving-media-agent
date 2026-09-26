"""隔离区清理的判据：哪一份此刻可以真删、哪一份只能留着。

隔离区是删除的唯一形态——`trash` 把文件移进来并记下逆操作。从这里再删一次就没有下一层保险了，
所以**按处置类别**决定怎么判（类别由删除关口在隔离那一刻记进审计的 `deletion.disposition`；
第 2 阶段之前的旧记录按规则推断，同一张表 `gate.disposition_for`）：

- **`extras`**（特典 / 菜单 / PV / OP-ED）：用户口径就是要删（2026-09-04 核对后的结论："留在隔离区，
  按 TRASH_RETENTION_DAYS=30 到期清除"）。过了保留期即可删。
- **`dead_partial`**（死种自己的 `.!qB` 半成品）：按定义不是可用拷贝。过了保留期即可删；
  只认 `.!qB`——第 1 阶段之前的死种处置搬的是 `content_path`，可能是完整正片甚至整季。
- **`duplicate`**：必须**证明**库里有它的替代者、而且替代者是完整的（下面两条）。
- **`bundled_version` / `manual` / `other`，以及没有隔离记录的**：永不自动删，交给人。

判重的两条证明：

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

`eligible` 只说"按判据此刻可以删"；**什么时候真删**由调用方（`disposal`）按模式定：`run` 只删
过了保留期的，`purge --apply` 是人要的、证明安全的判重可以提前放。

**身份来源**：`audit.jsonl` 的 `trash` 记录（`trashed_to` → 记录，最后一条为准）。`purge.jsonl` 里
人手工移入隔离区的（`{"op": "version_swap", "from", "to"}`）只用来说明它从哪来——没有审计记录，
就没有人按规则判过它是什么，一律 `other`。
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import gate
from .claims import PARTIAL
from .naming import VIDEO_EXTS, parse_episode
from .probe import duration as _duration, tail_decodes as _tail_decodes

_SXXEXX = re.compile(r"S(\d{1,2})E(\d{1,3})", re.IGNORECASE)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 永不自动删的处置类别：按定义不存在"库里的替代者"可证，删哪个由人定
HUMAN = ("bundled_version", "manual", "other")


@dataclass
class Candidate:
    """隔离区里的一份文件，及其能否安全删除的判定。"""
    trash_path: Path
    size: int
    disposition: str = "other"
    origin: str = ""          # 当初在库里的路径
    rule: str = ""            # 当初被哪条规则清理
    slot: tuple | None = None  # (季, 集)
    survivor: Path | None = None
    trashed_at: datetime | None = None
    age_days: float | None = None
    expired: bool = False     # 隔离已满 TRASH_RETENTION_DAYS
    eligible: bool = False    # 按它的处置类别，此刻可以真删
    why: str = ""
    record: dict | None = None


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
    它们说明了文件从哪来，但没有按规则判过它是什么，处置类别是 `other`。

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
        if not isinstance(r, dict):
            continue
        to = r.get("to") or ""
        if to.startswith(root) and r.get("from"):
            out[to] = r
    return out


def disposition_of_record(rec: dict | None) -> str:
    """审计记录的处置类别：新记录用删除关口记下的 `deletion.disposition`；旧记录（没有这个字段，
    9,619 行）按规则 / kind 推断，与关口同一张表。没有记录 → `other`。"""
    if not rec:
        return "other"
    d = (rec.get("deletion") or {}).get("disposition")
    if d in gate.DISPOSITIONS:
        return d
    return gate.disposition_for(rec.get("rule") or "", rec.get("kind") or "")


def _trashed_at(rec: dict | None, p: Path, trash_root: Path) -> datetime | None:
    """隔离时间：审计记录的 `ts`；没有就用隔离区日目录（`YYYY-MM-DD`）的日期。都没有返回 None。"""
    ts = (rec or {}).get("ts")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    try:
        day = p.relative_to(trash_root).parts[0]
    except (ValueError, IndexError):
        return None
    if _DAY.match(day):
        try:
            return datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _slot_of(name: str, summary: str, season_hint: int | None = None) -> tuple | None:
    """从文件名取 (季, 集)；取不到就退回摘要里的 `S03E09 重复：…`。

    集号解析走 `naming.parse_episode`，不要自己写正则——发布组的写法远不止
    `SxxExx` 一种，`[06]`、`- 06 [1080p]`、`第06话` 都很常见。

    `parse_episode` 可能只给出集号、给不出季号（`[06]` 这种形式本来就没有季）。
    这时用 `season_hint`；给不出就返回 None：**猜季号会让文件对到错误的集位上**。
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


class _Pool:
    """一次 `build_pool` 的上下文：配置、此刻、按需建的索引。"""

    def __init__(self, ctx, now: datetime):
        self.ctx = ctx
        self.cfg = ctx.config
        self.now = now
        self.media_root = Path(self.cfg.media_root)
        self.trash_root = Path(self.cfg.trash_dir)
        self.retention = float(self.cfg.trash_retention_days)
        self._sizes: dict | None = None
        self.dur_cache: dict = {}

    def sizes(self) -> dict:
        if self._sizes is None:
            self._sizes = _torrent_size_index(self.ctx.qbit)
        return self._sizes


def build_pool(ctx, *, now: datetime | None = None) -> list[Candidate]:
    """扫描隔离区，逐份给出处置类别与"此刻能否安全删除"。只读：不删、不写任何东西。"""
    pool = _Pool(ctx, now or datetime.now())
    audit = _audit_by_trash_path(Path(pool.cfg.audit_log))
    manual = _manual_by_trash_path(Path(pool.cfg.state_dir) / "purge.jsonl", pool.trash_root)

    out: list[Candidate] = []
    for p in sorted(pool.trash_root.rglob("*")):
        if p.name.startswith("._"):
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue                    # 目录、符号链接：只逐个处置普通文件
        c = Candidate(trash_path=p, size=st.st_size)
        rec = audit.get(str(p))
        if rec:
            c.record = rec
            c.rule = rec.get("rule") or ""
            c.origin = (rec.get("args") or {}).get("path") or ""
            c.disposition = disposition_of_record(rec)
        else:
            man = manual.get(str(p))
            if man:
                c.rule = "（%s，purge.jsonl）" % (man.get("op") or "手工")
                c.origin = man.get("from") or ""
        c.trashed_at = _trashed_at(rec, p, pool.trash_root)
        if c.trashed_at is not None:
            c.age_days = (pool.now - c.trashed_at).total_seconds() / 86400
            c.expired = c.age_days >= pool.retention
        _judge(pool, c)
        out.append(c)
    return out


def _judge(pool: _Pool, c: Candidate) -> None:
    if c.trashed_at is None:
        c.why = "隔离时间不明（没有审计记录，也不在日期目录下），不自动删"
        return
    d = c.disposition
    if d in HUMAN:
        c.why = _human_why(c)
        return
    if d == "duplicate":
        _prove_duplicate(pool, c)
        return
    left = pool.retention - (c.age_days or 0)
    if d == "dead_partial" and not c.trash_path.name.endswith(PARTIAL):
        c.why = ("死种记录，但隔离的不是半成品（.!qB）——第 1 阶段之前的死种处置搬的是 content_path，"
                 "可能是完整的正片，交给人")
        return
    label = "特典 / 菜单 / PV" if d == "extras" else "死种的半成品（.!qB）"
    if not c.expired:
        c.why = f"{label}：还在保留期里（隔离 {c.age_days:.1f} 天，保留 {pool.retention:g} 天，还剩 {left:.1f} 天）"
        return
    c.eligible = True
    c.why = f"{label}（{c.rule}）已过保留期 {pool.retention:g} 天（隔离 {c.age_days:.0f} 天）"


def _human_why(c: Candidate) -> str:
    if c.disposition == "bundled_version":
        return "合并发布的另一版本：用户口径只留一份，但留哪份由人定，不自动删（需人工处置）"
    if c.disposition == "manual":
        return "手写的删除（manual）：没有检测器的判据可复核，不自动删（需人工处置）"
    if c.record is None:
        return f"没有隔离记录（{c.rule or '来历不明'}），没人按规则判过它是什么，不自动删（需人工处置）"
    return f"规则 {c.rule or '（未知）'} 的删除：没有可复核的判据，不自动删（需人工处置）"


def _prove_duplicate(pool: _Pool, c: Candidate) -> None:
    rec = c.record or {}
    c.slot = _slot_of(Path(c.origin).name, rec.get("summary") or "")
    if not c.slot:
        c.why = "解析不出集号"
        return

    # 集位要在**整部番的各季目录**里找，不能只看原路径的父目录。
    #
    # 两种情况会让"只看父目录"给出错误答案：
    #   - 文件当初在库内的 `.other` / `.extras` 隔离子目录里（那不是集位，
    #     真正的正片在 `Season N/` 下）——实测《义妹生活》S01E07、
    #     《药屋少女的呢喃》S01E24 都因此被误报成"库里是空的，
    #     这份可能是唯一原件"，而它们的正片明明都在。
    #   - 整部番按 TMDB 重编排过（药屋的 Season 2 并进了 Season 1）。
    # 误报比漏报更伤：这道检查一旦开始喊狼来了，就没人再信它。
    media_root = pool.media_root
    show_dir = Path(c.origin)
    while show_dir.parent != media_root and show_dir.parent != show_dir:
        show_dir = show_dir.parent
    if not show_dir.is_dir():
        c.why = "原剧集目录已不存在（可能被移动或改名过）"
        return

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
        return
    if len(holders) > 1:
        c.why = (f"库里 S{sn:02d}E{ep:02d} 有 {len(holders)} 个文件，先解决重复再说")
        return

    # 条件 2：占位者必须被证明是完整文件。两条路径，满足其一即可。
    surv = holders[0]
    c.survivor = surv
    want = pool.sizes().get(str(surv))
    if want is not None:
        # 路径 a：种子校验。最强的证据——种子声明多少字节就该有多少字节。
        declared, progress = want
        actual = surv.stat().st_size
        if progress < 1.0:
            c.why = f"替代者的种子只下到 {progress*100:.1f}%"
            return
        if actual != declared:
            c.why = (f"替代者大小与种子声明不符（磁盘 {actual}，种子 {declared}）")
            return
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
            return
        med = _season_median_duration(show_dir, sn, surv, pool.dur_cache)
        if med is None:
            c.why = (f"库内替代者没有种子，同季也不足 2 集可作时长参照"
                     f"（替代者 {d_surv:.0f}s）")
            return
        # 判据只卡两个方向的**大幅**偏离，不要求"和中位数差不多"。
        #
        # 原先是 `abs(d - med) > max(30s, 2%)` 双向紧卡，实测三处全是误报：
        #   恶魔的破坏 S01 前八集 1430s、后五集 1450~1511s（后段片尾更长），
        #     中位数落在 1430，于是后段每一集都被判"偏离"；
        #   药屋按 TMDB 合并成 48 集后，E01-E24 是 1372s、E25-E48 是 1440s，
        #     中位数卡在两者之间，两个季末集双双中枪。
        # 「同季每集等长」这个假设，在分段规格和合并季面前都不成立。
        #
        # 真正要挡的是两类，都只在**大幅**偏离时才出现：
        #   偏短——截断的半成品，或是 PV/菜单占了集位；
        #   偏长——合集包占了集位（义妹生活 E01 那个 4307s 三集连播先行版
        #          就是这个形态，1430s 的三倍）。
        # 细微的正常波动（±10% 以内）不该拦。另有 `_tail_decodes` 专门
        # 对付"容器头写着完整时长、数据其实没写完"那种偏短查不出来的截断。
        if d_surv < med * 0.85:
            c.why = (f"替代者时长 {d_surv:.0f}s 明显短于同季中位数 {med:.0f}s，"
                     f"疑似截断或并非正片")
            return
        if d_surv > med * 1.5:
            c.why = (f"替代者时长 {d_surv:.0f}s 远长于同季中位数 {med:.0f}s，"
                     f"疑似合集包占了集位")
            return
        if not _tail_decodes(surv, d_surv):
            c.why = (f"替代者时长看着正常（{d_surv:.0f}s）但尾部解不出画面，"
                     f"疑似写入未完成的空壳")
            return
        complete_why = (f"无种子可校验，改以时长自证：{d_surv:.0f}s 与同季中位数 "
                        f"{med:.0f}s 相符，且尾部可解码")

    c.eligible = True
    c.why = f"S{sn:02d}E{ep:02d} 由 {surv.name} 占位，{complete_why}"
