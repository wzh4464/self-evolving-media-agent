"""隔离区清理的判据：哪一份此刻可以真删、哪一份只能留着。

隔离区是删除的唯一形态——`trash` 把文件移进来并记下逆操作。从这里再删一次就没有下一层保险了，
所以**按处置类别**决定怎么判（类别由删除关口在隔离那一刻记进审计的 `deletion.disposition`；
第 2 阶段之前的旧记录按规则推断，同一张表 `gate.disposition_for`）：

- **`extras`**（特典 / 菜单 / PV / OP-ED）：用户口径就是要删（2026-09-04 核对后的结论："留在隔离区，
  按 TRASH_RETENTION_DAYS=30 到期清除"）。过了保留期、按此刻再认一次仍是特典（名字认得出集号的，
  那一集库里另有可播的正片）即可删（`_extras_problem`）。
- **`dead_partial`**（死种自己的 `.!qB` 半成品）：按定义不是可用拷贝。过了保留期即可删；
  只认 `.!qB`——第 1 阶段之前的死种处置搬的是 `content_path`，可能是完整正片甚至整季。
- **`duplicate`**：必须**证明**库里有它的替代者、而且替代者是完整的（下面两条）。
- **`bundled_version` / `manual` / `other`，以及没有隔离记录的**：永不自动删，交给人。

判重的两条证明：

1. **库里有替代者，而且它"是这一集"可信。** 集位用检测器当时解析的那一个（`deletion.slot`，
   旧记录摘要里的 `SxxEyy 重复`），剧目录取自当初的路径；看库里**现在**有没有、且只有一个文件
   占着这个集位——"占着"与 duplicate-episode 分桶同一个定义（`builtin._resolve`：钉子优先、
   季号偏移照算，不是文件名正则）。查的是当前实际状态，不是信审计日志里记的"保留了谁"——
   日志是历史，文件可能后来又被换过。没钉子的替代者，名字要有名字之外的证据：发布名按同一套
   换算（季号偏移照算）也落在这一集上；没有种子的只有它就是删除关口记下的那个无种子保留方时
   才算（2026-08-31 Re:Zero，`_identity_problem`）。

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
from .claims import PARTIAL, fold
from .naming import parse_episode
from .probe import duration as _duration, tail_decodes as _tail_decodes

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
    # 评估那一刻的事实，unlink 之前按此刻复核（`recheck`）
    stat_key: tuple = ()      # 隔离文件的 (inode, 大小, mtime)
    survivor_hash: str = ""
    survivor_size: int | None = None


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


_SUMMARY_SLOT = re.compile(r"^S(\d{1,2})E(\d{1,4})(?!\d)")


def _slot_of(c: Candidate) -> tuple | None:
    """判重记录的集位：**检测器当时解析出来的那一个**，不从文件名重新猜。

    1. 新记录的 `deletion.slot`（删除关口记下：动作给的 slot > 钉子 > 名字）；
    2. 旧记录摘要开头的 `SxxEyy 重复：…`——duplicate-episode 按 `_resolve`（钉子、season_offsets、
       episode_offset）分桶时的集位；
    3. 都没有才看原文件名，而且名字里得明写季号——猜季号会对到错的集位上。

    以前是文件名优先：《超超超超超喜欢你的100个女朋友》第三季（episode_offset -24）的发布名
    `- 25` 会被当成第 25 集去找替代者。
    """
    rec = c.record or {}
    raw = (rec.get("deletion") or {}).get("slot")
    try:
        sn, ep = raw
        return int(sn), int(ep)
    except (TypeError, ValueError):
        pass
    m = _SUMMARY_SLOT.match(rec.get("summary") or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    sn, ep = parse_episode(Path(c.origin).name)
    if sn is None or ep is None:
        return None
    return sn, ep


def _show_dir_of(origin: str, media_root: Path) -> Path | None:
    """原路径所在的剧目录（媒体根下的第一层）；不在媒体库里返回 None。"""
    try:
        rel = Path(origin).relative_to(media_root)
    except ValueError:
        return None
    return media_root / rel.parts[0] if len(rel.parts) >= 2 else None


def _holders(show, slot) -> list:
    """此刻占着 `slot` 的库内文件——**与 duplicate-episode 分桶同一个定义**：视频、下完了、不是特典、
    不在 `.xxx` 已归置目录（scan 已分流进 `extras_files`）、盘上真的在（幻影不算），集位按
    `builtin._resolve`：钉子优先，其次文件名（发布名兜底），发布方声明的季号与库内不符又没有
    `season_offsets` 可换算的认不出（返回 None），AutoBangumi 的 `episode_offset` 照样换算。

    以前按文件名里的 `SxxEyy` 正则找：钉着 `ma:S01E58`、名字却叫 S01E08 的文件被认成 S01E08 的
    替代者；还没改名、只写 `- 05` 的替代者找不到。
    """
    from .plugins import builtin as B

    out, seen = [], set()
    for f in show.files:
        if not B._is_video(f) or f.is_incomplete or B._is_extra(f, show):
            continue
        if B.is_phantom(f) or not os.path.isfile(f.path) or os.path.islink(f.path):
            continue
        if B._resolve(f, show) != tuple(slot):
            continue
        k = fold(f.path)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def _identity_problem(f, show, slot, keeper: dict | None = None) -> str:
    """替代者"是这一集"可信吗？返回不可信的理由，可信返回空串。

    `_resolve` 认的是它此刻的名字（文件名优先）。名字可能是 AutoBangumi 按错的编号改的——
    分类交接之后、甚至种子被摘之后，名字照样在，所以**每一个没钉子的替代者都要有名字之外的
    正面证据**，不分分类：

    - **钉着 `ma:`**：抓取器按番组页 + 播出日期定的，是定论（`_resolve` 先认钉子，能走到这里
      就说明钉子就是这个集位）。
    - **有种子**：发布名（`renameFile` 改不动的显示名）按 `_resolve` 同一套换算（季号偏移、
      `episode_offset`）也得落在这个集位上（`builtin._release_slot`）。2026-08-31 Re:Zero：AB 把
      `3rd Season - 08` 改成 `S01E08`，判重把 2016 年真正的第 8 集清进隔离区；生产 sidecar 带着
      `season_offsets {"3": 50}`，以前"声明的季有偏移"就放行、从不换算——换算出来是 S01E58
      （2026-09-26 审查）。合集的发布名认不出单集的，成员叫什么只是某次改名的结果，同样不作保。
    - **没有种子**：发布名这份证据已经没了（保种到期、手动删种留文件——AB 改错名的那份摘了种子
      就是这个样子），名字无从核对，时长自证也分不出第 8 集和第 58 集。唯一的例外是它就是删除
      关口当初记下的保留方（`deletion.keeper`：内容摘要相同），而且那时它也没有种子——判重当时
      凭的就是这份证据，此后什么都没丢。
    """
    from .naming import declared_season
    from .plugins import builtin as B

    if B._pinned(f):
        return ""
    sn, ep = slot
    tag = f"S{sn:02d}E{ep:02d}"
    if f.torrent_hash:
        rs = B._release_slot(f, show)
        if rs == (sn, ep):
            return ""
        rel = (f.torrent_name or "")[:60]
        if f.torrent_category == "Bangumi":
            return (f"替代者 {f.filename} 还在 AutoBangumi 的分类下（改名权未交接），发布名也认不出"
                    f" {tag}——它叫这个名字只是 AB 认为的")
        if rs is not None:
            return (f"替代者 {f.filename} 的发布名（{rel}）按季号偏移换算是 S{rs[0]:02d}E{rs[1]:02d}，"
                    f"不是 {tag}——名字里的集号是按别的编号改的（2026-08-31 Re:Zero 的形态）")
        dec = declared_season(f.torrent_name or "")
        if dec is not None and dec != sn and str(dec) not in B._season_offsets(show):
            return (f"替代者 {f.filename} 的发布名声明的是第 {dec} 季（{rel}），"
                    f"库内是第 {sn} 季、又没有 season_offsets 可换算——名字里的集号可能是按那一季编的")
        return (f"替代者 {f.filename} 的发布名（{rel}）认不出 {tag}（合集、或没写集号），没钉子："
                f"它叫这个名字只是某次改名的结果，不作保、交给人")
    kp = keeper or {}
    if kp.get("digest") and not kp.get("hash"):
        from .dedup import content_digest
        if content_digest(Path(f.path)) == kp["digest"]:
            return ""
    return (f"替代者 {f.filename} 没有种子也没钉子：名字是谁定的无从核对（AB 改错名的文件摘了"
            f"种子就是这个样子，2026-08-31 Re:Zero），也不是删除关口当初记下的那个无种子保留方——"
            f"不作保、交给人")


class _Pool:
    """一次 `build_pool` 的上下文：配置、此刻、按需建的库快照。"""

    def __init__(self, ctx, now: datetime, early: bool = True):
        self.ctx = ctx
        self.early = early
        self.cfg = ctx.config
        self.now = now
        self.media_root = Path(self.cfg.media_root)
        self.trash_root = Path(self.cfg.trash_dir)
        self.retention = float(self.cfg.trash_retention_days)
        self.min_age = float(self.cfg.quarantine_min_age_days)
        self._shows: dict | None = None
        self.scan_errors: list[str] = []
        self.dur_cache: dict = {}
        self._claims = None

    def claims(self):
        """此刻的路径占用索引（`claims.ClaimIndex`）。`qbit=None` 时每一问都是"不知道"。"""
        if self._claims is None:
            from .claims import ClaimIndex
            self._claims = ClaimIndex(self.ctx.qbit)
        return self._claims

    def shows(self) -> dict:
        """此刻的库（`scan.build_state`，不查 TMDB）：`{fold(剧目录): Show}`。只有要证明判重时才扫，
        一次 build_pool 只扫一次。读 qBittorrent 不全记在 `scan_errors`。"""
        if self._shows is None:
            from .scan import build_state
            st = build_state(self.ctx, resolve_tmdb=False)
            self.scan_errors = list(st.qbit_errors)
            self._shows = {fold(s.dir_path): s for s in st.shows}
        return self._shows


def build_pool(ctx, *, now: datetime | None = None, early: bool = True) -> list[Candidate]:
    """扫描隔离区，逐份给出处置类别与"此刻能否安全删除"。只读：不删、不写任何东西。

    `early=False`：保留期内的判重不去证明（记"还在保留期里"、不算可删）。`run` 在空间充足时这样用——
    它们这一轮反正不删，证明要重扫整个库、探测、读头尾摘要，每 6 小时白做一遍。"""
    pool = _Pool(ctx, now or datetime.now(), early)
    audit = _audit_by_trash_path(Path(pool.cfg.audit_log))
    manual = _manual_by_trash_path(Path(pool.cfg.state_dir) / "purge.jsonl", pool.trash_root)

    from .actions import Executor

    out: list[Candidate] = []
    for p in sorted(pool.trash_root.rglob("*")):
        if Executor._is_junk(p.name):
            continue                    # 访达的 .DS_Store、AppleDouble 的 `._*`：不是被隔离的东西
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue                    # 目录、符号链接：只逐个处置普通文件
        c = Candidate(trash_path=p, size=st.st_size,
                      stat_key=(st.st_ino, st.st_size, st.st_mtime_ns))
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
    if (c.age_days or 0) < pool.min_age:
        # 不管判据多有把握：刚隔离的这一批还可能被整批回退（restore_from_trash 要它在）
        c.why = (f"隔离才 {c.age_days:.1f} 天，不到最短隔离期 {pool.min_age:g} 天"
                 f"（QUARANTINE_MIN_AGE_DAYS）：这一批还可能要回退")
        return
    if d == "duplicate":
        if not c.expired and not pool.early:
            c.why = (f"判重：还在保留期里（隔离 {c.age_days:.1f} 天，保留 {pool.retention:g} 天）；"
                     f"空间充足，run 不提前删，这一轮也不去证明")
            return
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
    why = _origin_problem(pool.claims(), c) or (_extras_problem(pool, c) if d == "extras" else "")
    if why:
        c.why = why
        return
    c.eligible = True
    c.why = f"{label}（{c.rule}）已过保留期 {pool.retention:g} 天（隔离 {c.age_days:.0f} 天）"


def _extras_problem(pool: _Pool, c: Candidate) -> str:
    """特典到期硬删之前，按**此刻**再认一次：它还是特典吗？名字认得出集号的，那一集此刻有没有
    可播的正片？返回不删的理由，没问题返回空串。

    特典规则认不出罗马音标题里的记号（`[G] Trailer Park Boys - 05`），靠"某集唯一的文件不当特典"
    兜底；兜底曾被一个幻影骗过、关口也曾对没钉子的特典不问集位（2026-09-26 审查）。隔离区是最后
    一层：以前特典到期就删，不看任何现场——唯一的那一集就这么没了。

    - 按现在的判据（`is_extra_of`：只看剧名之后的部分）它不是特典 → 不删、交给人（第 2 阶段之前
      整名匹配，标题带「菜单」的番把正片当特典隔离过）；
    - 集位：新记录用关口记下的 `deletion.slot`（检测器按 `_resolve` 算的 / 钉子 / 名字），旧记录按
      `_resolve` 重新认；认得出集号、库里那一集此刻没有可播的正片（`_holders`：下完了、不是特典、
      不是幻影、盘上真在）→ 它可能就是那一集，不删；
    - 重扫看不全 → 不删。
    """
    from .kernel import MediaFile
    from .naming import is_extra_of
    from .plugins import builtin as B

    name = Path(c.origin).name if c.origin else c.trash_path.name
    show_dir = _show_dir_of(c.origin, pool.media_root)
    if show_dir is None:
        return f"特典的原路径不在媒体库的某部番目录下（{c.origin or '（空）'}），认不出它此刻还是不是特典"
    shows = pool.shows()
    if pool.scan_errors:
        return (f"看不全：重扫时 qBittorrent 数据不完整（{pool.scan_errors[0]}），"
                f"确认不了它此刻仍是特典")
    show = shows.get(fold(show_dir))
    titles = B._titles(show) if show is not None else [show_dir.name]
    if not is_extra_of(name, titles):
        return (f"按现在的判据它不是特典：{name} 去掉剧名（{titles[0]}）之后没有特典记号——当初是按"
                f"整个名字判的，不按特典到期删，交给人")
    rel = Path(c.origin).relative_to(show_dir)
    season_dir = rel.parts[0] if len(rel.parts) > 1 else ""
    dele = (c.record or {}).get("deletion")
    if dele is not None:
        slot = gate._slot_arg(dele.get("slot"))
    elif show is not None:
        slot = B._resolve(MediaFile(path=Path(c.origin), size=c.size, show_dir=show_dir.name,
                                    season_dir=season_dir, filename=name), show)
    else:
        slot = gate.name_slot(Path(c.origin), season_dir)
    if slot is None:
        return ""
    c.slot = slot
    if show is not None and _holders(show, slot):
        return ""
    return (f"特典 {name} 的名字认得出 S{slot[0]:02d}E{slot[1]:02d}，而库里这一集此刻没有可播的正片——"
            f"它可能就是那一集（发布名的标题里带记号的正片），不删、交给人")


def _origin_problem(claims, c: Candidate, surv_path: Path | None = None,
                    surv_hash: str = "") -> str:
    """隔离文件**当初的路径**此刻还被种子以优先级非 0 声明吗？声明着就返回不删的理由。

    旧 purge 的 `.!qB` 检查拿**隔离区里的路径**去比种子的条目——没有任何种子指向 `state/trash`
    （测绘：生产 539 个种子，0 个条目在隔离区下），那道检查永远通过。真正该问的是原路径：

    - 隔离没做完：第 1 阶段之前 `qbit.delete` / 设为不下载失败只记一行日志，文件照样搬走——种子
      还要这个文件，删了它，种子就指着一个不存在的文件（qBittorrent 重下或报 missingFiles）；
    - 又有种子要往那写（换源抓来的新种子、死种半成品的同名新下载）。

    两种都该留着给人看。优先级 0 的声明不算：`file_only` 隔离之后种子照样列着那个条目，那正是
    隔离做完了的样子。替代者自己的种子声明着原路径也不算（输家当初就叫规范名，赢家同一批改名到了
    这个名字上）。`.!qB` 按它的正名问（种子声明的是 `X`）。qBittorrent 问不了 → 不删。
    """
    if not c.origin:
        return "原路径不明（审计记录里没有 path），无法确认有没有种子还要它"
    base = c.origin[: -len(PARTIAL)] if c.origin.endswith(PARTIAL) else c.origin
    own_hash, own_path = "", None
    if surv_path is not None and surv_hash and fold(surv_path) == fold(base):
        own_hash, own_path = surv_hash, surv_path
    chk = claims.check(base, own_hash=own_hash, own_path=own_path, disk=False)
    if chk.unknown:
        return f"无法确认原路径此刻有没有种子声明（{chk.unknown}），不删"
    if chk.claimants:
        return (f"原路径仍被种子以优先级非 0 声明（{chk.describe()}）：当初的隔离没做完，或又有种子"
                f"要往那里写——删了它，那个种子就指着一个不存在的文件")
    return ""


def _human_why(c: Candidate) -> str:
    if c.disposition == "bundled_version":
        return "合并发布的另一版本：用户口径只留一份，但留哪份由人定，不自动删（需人工处置）"
    if c.disposition == "manual":
        return "手写的删除（manual）：没有检测器的判据可复核，不自动删（需人工处置）"
    if c.record is None:
        return f"没有隔离记录（{c.rule or '来历不明'}），没人按规则判过它是什么，不自动删（需人工处置）"
    return f"规则 {c.rule or '（未知）'} 的删除：没有可复核的判据，不自动删（需人工处置）"


def _season_median_duration(pool: _Pool, show, sn: int, exclude: Path) -> float | None:
    """同一季其他集时长的中位数；不足 2 集参照就返回 None。

    这是"替代者没有种子"时唯一还站得住的完整性标尺：同一部番同一季的正片
    时长高度一致，截断的那份会明显偏短。参照必须**排除替代者自己**，
    否则它自己会把中位数拉过去。同季与否按检测器的集位解析认（与找替代者同一个定义）。
    """
    from .plugins import builtin as B

    key = (fold(show.dir_path), sn)
    if key not in pool.dur_cache:
        ds = []
        for f in show.files:
            if not B._is_video(f) or f.is_incomplete or B._is_extra(f, show):
                continue
            if not os.path.isfile(f.path):
                continue
            r = B._resolve(f, show)
            if not r or r[0] != sn:
                continue
            d = _duration(f.path)
            if d:
                ds.append((fold(f.path), d))
        pool.dur_cache[key] = ds
    ds = [d for p, d in pool.dur_cache[key] if p != fold(exclude)]
    if len(ds) < 2:
        return None
    ds.sort()
    n = len(ds)
    return ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) / 2


def _prove_duplicate(pool: _Pool, c: Candidate) -> None:
    c.slot = _slot_of(c)
    if not c.slot:
        c.why = "解析不出集号"
        return

    # 集位要在**整部番**里找，不能只看原路径的父目录：文件当初可能在库内的 `.other` / `.extras`
    # 隔离子目录里（《义妹生活》S01E07、《药屋少女的呢喃》S01E24 曾因此被误报成"唯一原件"），
    # 整部番也可能按 TMDB 重编排过（药屋的 Season 2 并进了 Season 1）。
    show_dir = _show_dir_of(c.origin, pool.media_root)
    if show_dir is None:
        c.why = f"原路径不在媒体库的某部番目录下：{c.origin or '（空）'}"
        return
    if not show_dir.is_dir():
        c.why = "原剧集目录已不存在（可能被移动或改名过）"
        return
    shows = pool.shows()
    if pool.scan_errors:
        c.why = (f"看不全：重扫时 qBittorrent 数据不完整（{pool.scan_errors[0]}），"
                 f"证明不了替代者")
        return
    show = shows.get(fold(show_dir))

    # 条件 1：库里现在有且只有一个文件占着这个集位，而且它"是这一集"可信
    sn, ep = c.slot
    holders = _holders(show, c.slot) if show is not None else []
    if not holders:
        c.why = f"库里 S{sn:02d}E{ep:02d} 现在是空的——这份可能是唯一的原件"
        return
    if len(holders) > 1:
        c.why = (f"库里 S{sn:02d}E{ep:02d} 有 {len(holders)} 个文件，先解决重复再说")
        return
    surv = holders[0]
    c.survivor, c.survivor_hash = surv.path, surv.torrent_hash
    why = _identity_problem(surv, show, c.slot,
                            ((c.record or {}).get("deletion") or {}).get("keeper"))
    if why:
        c.why = why
        return

    # 条件 2：替代者必须被证明是完整文件。两条路径，满足其一即可。
    actual = os.path.getsize(surv.path)
    c.survivor_size = actual
    if surv.torrent_hash:
        # 路径 a：种子校验。最强的证据——种子声明多少字节就该有多少字节。
        declared, progress = surv.size, float(surv.torrent_progress or 0)
        if progress < 1.0:              # 纵深防御：`_holders` 已经不收没下完的，这里只在它改了之后才会走到
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
        d_surv = _duration(surv.path)
        if d_surv is None:
            c.why = "库内替代者没有种子，且读不出时长，无法确认完整"
            return
        med = _season_median_duration(pool, show, sn, surv.path)
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
        if not _tail_decodes(surv.path, d_surv):
            c.why = (f"替代者时长看着正常（{d_surv:.0f}s）但尾部解不出画面，"
                     f"疑似写入未完成的空壳")
            return
        complete_why = (f"无种子可校验，改以时长自证：{d_surv:.0f}s 与同季中位数 "
                        f"{med:.0f}s 相符，且尾部可解码")

    why = (_rank_problem(c, surv, show)
           or _origin_problem(pool.claims(), c, surv.path, surv.torrent_hash))
    if why:
        c.why = why
        return
    c.eligible = True
    c.why = f"S{sn:02d}E{ep:02d} 由 {surv.filename} 占位，{complete_why}"


def _rank_problem(c: Candidate, surv, show) -> str:
    """I4 复排：隔离的这份按**现在的**规则该不该赢？该赢（或它自己封存着这一集）就返回不删的理由。

    判重的取舍规则一直在改（探到的字幕轨压过名字、体积跨编码折算、偏好分……），隔离区里躺着的是
    按**当时的**规则判输的。只证明"库里那份完整"不够：2026-09-08 穹庐下的魔女 S01E11，带两条中文
    内封字幕的那份当时输在体积上，被清进隔离区、当天人工捞回——按现在的排序它才是该留的。

    与判重同一口径（`builtin`）：
    - 隔离的这份钉着这一集（删除那一刻记下的 `deletion.subject`）且复核通过 → 它封存着这一集，不删；
      **探测不可用当作封存**（与删除关口 I4 相同）。两份都封存时删哪个由人定。
    - 库里那份钉着这一集且复核通过 → 封存不拿画质跟后来的比，照删。
    - 否则 `_rank_for_keep`（分辨率、探到的字幕能力、BDRip、跨编码折算的体积）隔离的这份更高 → 不删。
    - 两份字节相同（`dedup.content_digest`）→ 谁留都一样，不比。
    """
    from .dedup import content_digest
    from .kernel import MediaFile
    from .naming import parse_pin
    from .plugins import builtin as B
    from .probe import probe

    sub = ((c.record or {}).get("deletion") or {}).get("subject") or {}
    name = Path(c.origin).name if c.origin else c.trash_path.name
    mine = MediaFile(path=c.trash_path, size=c.size, show_dir=show.dir_name,
                     season_dir=surv.season_dir, filename=name,
                     torrent_hash=sub.get("torrent_hash") or "",
                     torrent_name=sub.get("name") or "", torrent_progress=1.0,
                     torrent_tags=sub.get("tags") or "",
                     torrent_category=sub.get("category") or "")
    da, db = content_digest(c.trash_path), content_digest(surv.path)
    if da and db and da == db:
        return ""
    tag = "S%02dE%02d" % tuple(c.slot)
    pin = parse_pin(sub.get("tags") or "") or parse_pin(f"ma:{sub.get('pin') or ''}")
    if pin and tuple(pin) == tuple(c.slot):
        ok, why = B.meets_requirements(mine)
        if probe(c.trash_path) is None:
            return (f"隔离的这份钉着 ma:{tag}，探测不可用、复核结论不可知——封存按「已封存」处理，"
                    f"不删（I4，交给人挑）")
        if ok:
            return (f"隔离的这份钉着 ma:{tag} 且复核通过（{why}），它封存着这一集——"
                    f"两份都封存时留哪份由人定，不删（I4）")
    if B._pinned(surv) == tuple(c.slot) and B.meets_requirements(surv)[0]:
        return ""
    if B._rank_for_keep(mine) > B._rank_for_keep(surv):
        return (f"按现在的排序，隔离的这份比库里的 {surv.filename} 更该留（I4 复排：判重当时的规则"
                f"与现在不同，如 2026-09-08 穹庐 S01E11），不删、交给人")
    return ""


def recheck(ctx, c: Candidate) -> str:
    """unlink 之前的最后一问：评估之后，这一份的前提还在吗？还在返回空串，否则返回不删的理由。

    评估（重扫整个库、探测、算摘要）到逐个 unlink 之间可能隔着几分钟；运行锁挡得住别的 media-agent，
    挡不住 AutoBangumi（每 60 秒改名一次）、qBittorrent、用户。只复核评估时依赖的**此刻的事实**，
    每一份用一个新的占用索引（不复用评估时的缓存）：

    - 隔离文件还是评估时那一个（inode、大小、mtime 都没变，仍是普通文件）；
    - 判重的替代者还在原处、大小没变；有种子的，种子还在、下完了、仍以优先级非 0 声明它；
    - 原路径此刻没有种子以优先级非 0 声明（第 5 节，替代者自己的除外）。
    """
    from .claims import ClaimIndex, ClaimsUnknown

    try:
        st = os.lstat(c.trash_path)
    except OSError:
        return "隔离文件在评估之后已不在"
    if not stat.S_ISREG(st.st_mode) or (st.st_ino, st.st_size, st.st_mtime_ns) != c.stat_key:
        return "隔离文件在评估之后变了（被替换或改写过），这次不删、下一轮重新评估"
    claims = ClaimIndex(ctx.qbit)
    if c.survivor is not None:
        try:
            sst = os.lstat(c.survivor)
        except OSError:
            return f"替代者 {c.survivor.name} 在评估之后不见了（被改名、挪走或删掉）"
        if not stat.S_ISREG(sst.st_mode) or sst.st_size != c.survivor_size:
            return f"替代者 {c.survivor.name} 在评估之后变了（大小 {c.survivor_size} → {sst.st_size}）"
        if c.survivor_hash:
            try:
                t = claims.torrent(c.survivor_hash)
                entries = claims.entries(c.survivor_hash) if t is not None else []
            except ClaimsUnknown as e:
                return f"无法确认替代者的种子此刻的状态（{e}），不删"
            if t is None:
                return f"替代者的种子 {c.survivor_hash[:8]} 在评估之后被摘掉了"
            if float(t.get("progress") or 0) < 1:
                return f"替代者的种子 {c.survivor_hash[:8]} 在评估之后又没下完了"
            sp = Path((t.get("save_path") or "").rstrip("/") or "/")
            e = next((e for e in entries if fold(sp / e["name"]) == fold(c.survivor)), None)
            if e is None or e.get("priority", 1) == 0:
                return f"替代者的种子 {c.survivor_hash[:8]} 已不再声明 {c.survivor.name}"
    return _origin_problem(claims, c, c.survivor, c.survivor_hash)
