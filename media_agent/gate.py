"""删除关口（deletion gate）：`trash` / `drop_torrent` 生效之前的最后一道闸。

**为什么要一道统一的关口。** 删除类动作由好几个检测器产出（duplicate-episode 的输家与
合并发布的兄弟、extras-in-library、dead-torrent、colliding-torrent，还有人手写的 `manual`），
每个检测器各自把关、各有各的洞（整改前的只读测绘逐条列过）：

- 判重的输家若在多文件合集里，执行器整种子作废——其余十几集失去做种、回退加不回来
  （生产审计 63 条 duplicate-episode 的整种子作废，例：3年Z组银八老师 [01-12]）；
- 保留方是幻影、被截断、或同一批里先被别的动作删掉了，输家照删；
- 另一个种子仍声明着要删的那个路径，scan 每个路径只出一条、看不见它；
- 钉了 `ma:` 的封存集位，探测一超时就"失封"，第二天被当输家删掉；
- 标题里带 `trailer` / `菜单` 的番，特典规则会把每一集都当特典。

检测器的判断发生在**诊断那一刻**、基于一份快照；删除发生在几分钟之后，中间还夹着同一批
的其它动作。所以不变量要在**执行器里、动手前的那一刻、按此刻的 qBittorrent 与磁盘**复核，
按**目标本身**（路径 + 种子 hash）认，与产出它的是哪个检测器、`Finding.key()` 是什么无关
（critic N7：两个死种的 finding 曾因 key 相同塌成一条）。

**不变量**（任何一条不满足就拒绝；拒绝记 skipped，理由以「删除关口：Ix」开头——
它们不是失败，不该污染 `find_failure_patterns`）：

- **I1 不让任何集位变成零个可播文件。**
  - 动作点名了保留方（判重：`keep_path` / `keep_hash` / `slot`）：保留方此刻在盘上、是普通
    文件、下完了（种子进度 1、不是 `.!qB`、盘上大小不小于种子声明的；纯本地的不小于诊断时的
    大小）、本批次没有被删掉（路径没进隔离区、种子没被摘）、仍然归这个集位（钉子 / 显式
    `SxxEyy` 没变）；要删的那个也仍然归这个集位。
  - 没点名保留方（特典、死种半成品、手写的删除）：要删的若是某个集位**唯一**的可播文件就拒绝——
    除非这是明确的特典 / 半成品处置，且它没钉 `ma:`、也没封存。
  - `drop_torrent`（只摘记录）：撞车的受害者被摘之前，保留方此刻仍以优先级非 0 声明那个共享的
    文件、文件在盘上、没被截断（`check_drop`）。
- **I2 不删另一个保留着的种子仍然声明的路径**（`claims.ClaimIndex.check(disk=False)`，
  豁免这次动作自己要处置的那个种子的那个条目；本批次已摘的种子不算）。
- **I3 不为了去掉一个文件整种子作废多文件种子**：所属种子此刻要下载的文件多于一个时，
  **自动降级**成只作废这一个条目（按 `save_path + 条目名` 的完整路径认），并在审计里记一笔；
  只剩这一个时反过来整种子作废（留一个什么都不下的空种子会被 stale-torrent-path 报成死链）。
- **I4 不删封存了集位的文件**：钉了 `ma:SxxEyy`、复核（`meets_requirements`）通过。**探测不可用
  （`probe` 返回 None：超时、出错）一律当作封存**——封存不能因为某一轮 ffprobe 超时就丢；
  例外是合并发布里同一个种子的兄弟文件：封存由判重选中的那一份（同一个种子、同一个钉子）持有，
  兄弟按用户偏好只留一份。钉子是整个种子的，特典处置的对象若不是那一集（钉着的合集里的 NCOP），
  它不持有封存。
- **死种半成品**（处置类别 `dead_partial`）：只许动 `.!qB`，而且那个死种此刻已不在 qBittorrent 里
  （同一批里被摘掉了，或已被删）——摘除没发生，它就仍在声明这份半成品（归入 I2）。
- 另外：**演进规则产出的删除一律不执行**（critic N5）。第 1 阶段在 `Executor._dispatch` 拦下
  所有演进动作，那道保留作纵深防御；删除的唯一执法点在这里（`screen`）。

**看不全就拒绝**：qBittorrent 读失败（`ClaimsUnknown`）记 failed「无法确认…占用情况，未做任何
改动」，与占用闸门同口径。

**审计**：`Verdict.audit()` 给每条 trash 记录加一个 `deletion` 字段（新键，旧记录没有它，
读的人要容忍缺失）——purge 要的事实在删除那一刻最全，之后种子可能已经摘了：

    "deletion": {
      "gate": "passed" | "I1" | "I2" | "I4" | "evolved" | "unknown" | "mismatch",
      "disposition": "duplicate" | "extras" | "bundled_version" | "dead_partial" | "manual" | "other",
      "rule": "<规则 id，同记录顶层的 rule>",
      "slot": [季, 集] | null,
      "keeper": {"path", "hash", "digest"} | null,
      "subject": {"torrent_hash", "name", "pin", "tags", "category", "torrent_files",
                  "removed_this_batch"?: true},     # 种子本批次早先已被摘：摘的那一刻记下的
      "notes": [...]?          # 例如 I3 的自动降级
    }

`gate` 不是 `passed` 的记录一定是 skipped（I1 / I2 / I4 / evolved）或 failed（unknown /
mismatch）；演进规则被拒的只有 `gate` / `disposition` / `rule`。drop_torrent 的记录同形
（`subject` 是被摘的种子，`keeper` 是撞车的保留方）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .claims import PARTIAL, ClaimsUnknown, fold
from .kernel import DSL_ORIGIN, Finding
from .naming import (VIDEO_EXTS, explicit_slot, is_extra, parse_episode, parse_pin,
                     season_of_dir)

PREFIX = "删除关口："

DISPOSITIONS = ("duplicate", "extras", "bundled_version", "dead_partial", "manual", "other")


def disposition_of(f: Finding) -> str:
    """这次删除属于哪一类处置。**只看产出它的规则**（内置检测器的 id / kind），
    不看动作参数——参数可以由任何人写（手工脚本、演进规则），而处置类别决定了
    I1 的例外（特典 / 半成品可以删掉某集位唯一的文件），不能让参数自己给自己放行。"""
    return disposition_for(f.rule, f.kind)


def disposition_for(rule: str, kind: str) -> str:
    """`disposition_of` 的规则表。隔离区处置（`purge`）拿它给没有 `deletion` 字段的旧审计记录
    推断处置类别——同一张表，不能两处各写一份。"""
    if rule == "duplicate-episode":
        return "bundled_version" if kind == "bundled_version" else "duplicate"
    if rule == "extras-in-library":
        return "extras"
    if rule == "dead-torrent":
        return "dead_partial"
    if rule == "manual":
        return "manual"
    return "other"


def screen(f: Finding) -> str:
    """不看任何现场就能拒绝的：演进规则（LLM 提议的 DSL）产出的删除。返回拒绝理由或空串。"""
    if (f.evidence or {}).get("origin") == DSL_ORIGIN:
        return (PREFIX + "演进规则产出的删除一律不执行（未经人工确认；critic N5），"
                f"规则 {f.rule}")
    return ""


@dataclass
class Verdict:
    """一次删除的关口结论，以及执行器该怎么做。"""
    disposition: str
    rule: str = ""                       # 产出这次删除的规则 id（与审计记录的 `rule` 相同）
    gate: str = "passed"                 # passed | I1..I4 | evolved | unknown | mismatch
    refused: str = ""                    # 非空 = 拒绝（skipped），已带「删除关口：」前缀
    failed: str = ""                     # 非空 = 看不全 / 参数与现场对不上（failed）
    torrent_hash: str = ""               # 此刻仍在 qBittorrent 里、要处置的那个种子；空 = 没有
    file_only: bool = False              # True = 只作废 `entry` 这一个条目
    entry: dict | None = None
    notes: list[str] = field(default_factory=list)
    slot: tuple[int, int] | None = None
    keeper: dict | None = None
    subject: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.failed

    def refuse(self, invariant: str, why: str) -> "Verdict":
        self.gate, self.refused = invariant, f"{PREFIX}{invariant} {why}"
        return self

    def fail(self, why: str, gate: str = "unknown") -> "Verdict":
        self.gate, self.failed = gate, why
        return self

    def audit(self) -> dict:
        d: dict = {"gate": self.gate, "disposition": self.disposition, "rule": self.rule,
                   "slot": list(self.slot) if self.slot else None,
                   "keeper": self.keeper, "subject": self.subject}
        if self.notes:
            d["notes"] = list(self.notes)
        return {"deletion": d}


# ------------------------------------------------------------------ 小工具
def _is_partial(p: Path) -> bool:
    return p.name.endswith(PARTIAL)


def _base(p: Path) -> Path:
    """`X.!qB` → `X`；其余原样。"""
    return Path(str(p)[: -len(PARTIAL)]) if _is_partial(p) else p


def _is_video(p: Path) -> bool:
    return _base(p).suffix.lower() in VIDEO_EXTS


def name_slot(p: Path, season_dir: str = "") -> tuple[int, int] | None:
    """只凭名字（与所在季目录——剧目录下的第一层）认出的集位；认不出返回 None。
    不看集号偏移：关口宁可认少（认少了 I1 更严，不会更松）。"""
    sn, ep = parse_episode(_base(p).name)
    if ep is None:
        return None
    if sn is None:
        sd = season_of_dir(season_dir)
        sn = sd if sd is not None else 1
    return sn, ep


def _show_dir(media_root: Path, p: Path) -> tuple[Path | None, str]:
    """`p` 所在的剧目录与季目录名（剧目录下的第一层；直接在剧目录下为空串）。"""
    try:
        rel = p.relative_to(media_root)
    except ValueError:
        return None, ""
    if len(rel.parts) < 2:
        return None, ""
    return media_root / rel.parts[0], (rel.parts[1] if len(rel.parts) > 2 else "")


def _fmt(slot) -> str:
    return "S%02dE%02d" % tuple(slot)


def _slot_arg(v) -> tuple[int, int] | None:
    try:
        s, e = v
        return int(s), int(e)
    except (TypeError, ValueError):
        return None


def _removed(ex) -> set[str]:
    """本批次已摘掉的种子（小写 hash；qBittorrent 给的本来就是小写，这里只是不信任调用方）。"""
    return {h.lower() for h in ex._removed_torrents}


def _entry_at(t: dict, entries: list[dict], abs_path: Path) -> dict | None:
    sp = Path((t.get("save_path") or "").rstrip("/") or "/")
    return next((e for e in entries if sp / e["name"] == abs_path), None)


# ------------------------------------------------------------------ trash
def check_trash(ex, f: Finding, path: Path) -> Verdict:
    """`path`（盘上真实存在的普通文件，调用方已核过）能不能移进隔离区，以及怎么处置它的种子。

    `ex` 是执行器：用它的占用索引（`_claims()`）与本批次已处置的种子 / 路径。
    返回的 `Verdict`：`refused` → 记 skipped；`failed` → 记 failed；否则按 `torrent_hash`
    / `file_only` / `entry` 处置种子，再搬文件。
    """
    a = f.action
    v = Verdict(disposition_of(f))
    why = screen(f)
    if why:
        v.gate, v.refused = "evolved", why
        return v
    claims = ex._claims()
    h = (a.args.get("torrent_hash") or "").lower()
    base = _base(path)
    try:
        t = claims.torrent(h) if h and h not in _removed(ex) else None
        entries = claims.entries(h) if t is not None else []
    except ClaimsUnknown as e:
        return v.fail(f"无法确认种子文件列表与路径占用情况，未做任何改动：{e}")
    wanted = [e for e in entries if e.get("priority", 1) != 0]
    media_root = Path(ex.cfg.media_root)
    pin = describe(v, f, path, h, t, wanted, media_root, ex._removed_subjects)

    # ---- 死种半成品：只许动 `.!qB`，而且那个死种必须已经不在了（同一批里被摘掉，或已被删）
    if v.disposition == "dead_partial":
        if not _is_partial(path):
            return v.refuse("I1", f"死种处置只许动半成品（.!qB），{path.name} 不是")
        if t is not None:
            return v.refuse("I2", f"所属的死种 {h[:8]} 此刻还在 qBittorrent 里（摘除没发生：又有了"
                                  f"做种、或有成员下完了），它仍声明着这份半成品")

    # ---- I3：种子的形状决定怎么处置它（先算出来，后面的检查都按它来）
    if t is not None:
        v.torrent_hash = h
        entry = _entry_at(t, entries, base)
        if entry is None:
            return v.fail("种子文件列表里找不到该文件（或不止一条匹配），拒绝只搬文件、不改种子",
                          gate="mismatch")
        v.entry = entry
        file_only = bool(a.args.get("file_only"))
        if len(wanted) > 1 and not file_only:
            file_only = True
            v.notes.append(i3_note(wanted, entry))
        elif len(wanted) <= 1:
            file_only = False                # 只剩它一个：设为不下载会留下一个空种子
        v.file_only = file_only

    # ---- I2：别的种子仍声明着这个路径
    try:
        chk = claims.check(base, own_hash=v.torrent_hash, own_path=base, disk=False)
    except ClaimsUnknown as e:               # check 自己会折成 unknown，这里只是保险
        return v.fail(f"无法确认路径的占用情况，未做任何改动：{e}")
    if chk.unknown:
        return v.fail(f"无法确认路径的占用情况，未做任何改动：{chk.unknown}")
    if chk.claimants:
        return v.refuse("I2", "另一个保留着的种子仍声明这个路径（" + chk.describe()
                        + "），删了它就少一个文件")

    # ---- I1：不让任何集位变成零个可播文件（半成品、字幕不是可播的正片）
    if _is_video(path) and not _is_partial(path):
        try:
            if v.keeper is not None:
                why = _keeper_problem(ex, v, f, path, pin, media_root)
            else:
                why = _last_copy_problem(ex, v, path, pin, media_root)
        except ClaimsUnknown as e:
            return v.fail(f"无法确认保留方 / 同集其它文件的占用情况，未做任何改动：{e}")
        if why:
            return v.refuse("I1", why)

    # ---- I4：封存了集位的文件不删；探测不可用 = 不知道 = 当作封存
    if pin and _is_video(path) and not _is_partial(path):
        why = _seal_problem(v, f, path, h, t, pin, wanted)
        if why:
            return v.refuse("I4", why)
    return v


def _seal_problem(v: Verdict, f: Finding, path: Path, h: str, t: dict, pin,
                  wanted: list[dict]) -> str:
    """要删的这个封存着集位吗？封存着就返回拒绝理由。

    封存 = 钉着 `ma:SxxEyy`（抓取器按偏好挑中的那一份）且复核（`meets_requirements`）通过。
    **探测不可用（`probe` 为 None：超时、出错）一律当作封存**：LoliHouse 的内部名只写
    `ASSx2`，只看名字过不了硬门槛（`tests/test_seal_slot.py` 第 3 组），某一轮 ffprobe 超时
    它就会失封、被当成输家——封存必须稳定。

    钉子是整个种子的，特典处置的对象若不是那一集（名字认不出钉着的集号、种子里还有别的视频，
    比如合集里的 NCOP），它不持有封存。唯一的例外是合并发布：保留方与要删的在同一个种子里、
    钉着同一个钉子——封存由判重选中的那一份持有，兄弟按用户偏好只留一份。
    """
    from .kernel import MediaFile
    from .plugins.builtin import meets_requirements
    from .probe import probe

    if v.disposition == "extras":
        videos = [e for e in wanted if _base(Path(e["name"])).suffix.lower() in VIDEO_EXTS]
        if len(videos) > 1 and parse_episode(path.name)[1] != pin[1]:
            return ""                         # 钉着的种子里的周边，不是那一集
    keeper_hash = (v.keeper or {}).get("hash") or ""
    if keeper_hash and keeper_hash == h:
        return ""                             # 合并发布的兄弟：封存在同种子的保留方手上
    mf = MediaFile(path=path, size=path.stat().st_size, show_dir="", season_dir="",
                   filename=path.name, torrent_hash=h, torrent_name=t.get("name", ""),
                   torrent_state=t.get("state", ""),
                   torrent_progress=float(t.get("progress") or 0),
                   torrent_tags=t.get("tags", ""), torrent_category=t.get("category", ""))
    info = probe(path)
    ok, why = meets_requirements(mf)
    if info is None:
        return (f"{path.name} 钉着 ma:{_fmt(pin)}，探测不可用（超时或出错），复核结论不可知——"
                f"封存按「已封存」处理，不删（{why}）")
    if ok:
        return (f"{path.name} 钉着 ma:{_fmt(pin)} 且复核通过（{why}），封存着这一集——"
                f"两个种子都封存时交给人挑，不替择源随便删一个")
    return ""


def _keeper_problem(ex, v: Verdict, f: Finding, path: Path, pin, media_root: Path) -> str:
    """点名了保留方（判重）：它此刻还替这个集位作保吗？返回拒绝理由，没问题返回空串。"""
    from .actions import _inside
    args = f.action.args
    slot = v.slot
    # 要删的这个此刻仍归这个集位（钉子、显式 SxxEyy 没变）
    mine = pin or explicit_slot(path.name)
    if slot and mine and tuple(mine) != tuple(slot):
        return (f"要删的文件此刻归 {_fmt(mine)}，不是 {_fmt(slot)}——它已不是这一集的重复"
                f"（诊断之后钉子或名字变了）")
    kp, bad = _inside(args.get("keep_path"), media_root, "keep_path")
    if bad:
        return f"保留方路径不合法：{bad}"
    if fold(kp) == fold(path):
        return (f"保留方与要删的是同一个文件（{path.name}）——「保留 X，清理 X」："
                f"两个种子声明同一路径时判重会把唯一的真文件当输家")
    if fold(kp) in {fold(p) for p in ex._trashed_paths}:
        return f"保留方 {kp.name} 本批次已被移进隔离区，{_fmt(slot) if slot else '这一集'} 会一个不剩"
    kh = (args.get("keep_hash") or "").lower()
    if kh and kh in _removed(ex):
        return f"保留方的种子 {kh[:8]} 本批次已被摘掉，它不再替这一集作保"
    if not os.path.lexists(kp) or kp.is_symlink() or not kp.is_file():
        return f"保留方 {kp.name} 此刻不在盘上（幻影，或诊断之后被挪走）"
    if _is_partial(kp):
        return f"保留方 {kp.name} 是半成品"
    size = kp.stat().st_size
    kpin = None
    claims = ex._claims()
    if kh:
        kt = claims.torrent(kh)
        if kt is None:
            return f"保留方的种子 {kh[:8]} 已不在 qBittorrent 里"
        prog = float(kt.get("progress") or 0)
        if prog < 1:
            return f"保留方的种子 {kh[:8]} 还没下完（{prog * 100:.1f}%）"
        ke = _entry_at(kt, claims.entries(kh), kp)
        if ke is None or ke.get("priority", 1) == 0:
            return f"保留方的种子 {kh[:8]} 已不再声明 {kp.name}（改过名或设为不下载）"
        if float(ke.get("progress", 1) or 0) < 1:
            return f"保留方 {kp.name} 在种子里还没下完"
        if size < int(ke.get("size") or 0):
            return (f"保留方 {kp.name} 盘上只有 {size} 字节，比种子声明的 {ke['size']} 少"
                    f"（截断或被替换）")
        kpin = parse_pin(kt.get("tags") or "")
    else:
        want = int(args.get("keep_size") or 0)
        if size <= 0 or size < want:
            return (f"保留方 {kp.name} 盘上只有 {size} 字节，比诊断时的 {want} 少"
                    f"（截断或被替换）")
    ks = kpin or explicit_slot(kp.name)
    if slot and ks and tuple(ks) != tuple(slot):
        return f"保留方此刻归 {_fmt(ks)}，不是 {_fmt(slot)}（诊断之后钉子或名字变了）"
    return ""


def _last_copy_problem(ex, v: Verdict, path: Path, pin, media_root: Path) -> str:
    """没点名保留方（特典、半成品、手写的删除）：要删的是不是某个集位唯一的可播文件。"""
    slot = v.slot
    if not slot:
        return ""                             # 认不出集位：不是某一集的正片
    if v.disposition in ("extras", "dead_partial") and not pin:
        return ""                             # 明确的特典 / 半成品处置，且没钉 ma:
    if other_holders(ex, path, slot, media_root):
        return ""
    why = f"{_fmt(slot)} 此刻只剩这一个可播文件"
    if pin:
        why += f"（种子钉着 ma:{_fmt(pin)}，是抓取器认定的正片）"
    return why + "，删了这一集就没了"


def other_holders(ex, path: Path, slot, media_root: Path) -> list[str]:
    """同一部番里，除 `path` 之外此刻还能播 `slot` 这一集的文件。

    算数的：剧目录下（不在 `.xxx` 隐藏目录里）的视频文件、不是半成品、不是特典、本批次没进
    隔离区；有种子声明的要那个条目下完了、盘上大小不小于声明的（种子本批次已摘的按纯本地算）；
    集位按种子的 `ma:` 钉子，没有就按名字。认不出集位的不算——宁可少算。
    """
    show_dir, _ = _show_dir(media_root, path)
    if show_dir is None or not show_dir.is_dir():
        return []
    claims = ex._claims()
    owners = _owners_under(claims, show_dir, _removed(ex))
    trashed = {fold(p) for p in ex._trashed_paths}
    me = fold(path)
    out = []
    for p in sorted(show_dir.rglob("*")):
        rel = p.relative_to(show_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if p.is_symlink() or not p.is_file() or not _is_video(p) or _is_partial(p):
            continue
        fp = fold(p)
        if fp == me or fp in trashed or is_extra(p.name):
            continue
        size = p.stat().st_size
        hpin = None
        claimers = owners.get(fp, [])
        if claimers:
            done = [(t, e) for t, e in claimers
                    if float(e.get("progress", 0) or 0) >= 1 and size >= int(e.get("size") or 0)]
            if not done:
                continue
            hpin = next((parse_pin(t.get("tags") or "") for t, _ in done
                         if parse_pin(t.get("tags") or "")), None)
        elif size <= 0:
            continue
        s = hpin or name_slot(p, rel.parts[0] if len(rel.parts) > 1 else "")
        if s and tuple(s) == tuple(slot):
            out.append(str(p))
    return out


def _owners_under(claims, show_dir: Path, removed) -> dict[str, list[tuple[dict, dict]]]:
    """`{fold(路径): [(种子视图, 条目), …]}`：此刻在 `show_dir` 之下有优先级非 0 条目的种子。"""
    fd = fold(show_dir)
    out: dict[str, list[tuple[dict, dict]]] = {}
    for h, t in claims.torrents().items():
        if h in removed:
            continue
        sp = (t.get("save_path") or "").rstrip("/")
        if not sp:
            continue
        fsp = fold(sp)
        if not (fsp == fd or fsp.startswith(fd + "/") or fd.startswith(fsp + "/")):
            continue
        for e in claims.entries(h):
            if e.get("priority", 1) == 0:
                continue
            out.setdefault(fold(Path(sp) / e["name"]), []).append((t, e))
    return out


def subject_of(h: str, t: dict | None, wanted_count: int | None) -> dict:
    """审计里"被删的是谁"：所属种子此刻的名字 / 钉子 / 标签 / 分类 / 要下载的文件数。"""
    pin = parse_pin((t or {}).get("tags") or "")
    return {"torrent_hash": h, "name": (t or {}).get("name", ""),
            "pin": _fmt(pin) if pin else None,
            "tags": (t or {}).get("tags", ""), "category": (t or {}).get("category", ""),
            "torrent_files": wanted_count if t is not None else None}


def describe(v: Verdict, f: Finding, path: Path, h: str, t: dict | None,
             wanted: list[dict], media_root: Path | None = None,
             removed: dict | None = None) -> tuple[int, int] | None:
    """把审计要的事实填进 `v`（`rule`、`subject`、`slot`、`keeper`），返回此刻种子上的 `ma:` 钉子。

    集位：动作给的 `slot`（检测器解析过偏移、钉子）> 此刻的钉子 > 名字。
    所属种子本批次早先已被摘掉时（`removed`：执行器摘除那一刻记下的 subject），审计照样记下它
    是谁——purge 要的事实在删除那一刻最全，之后就查不到了。"""
    args = f.action.args
    v.rule = f.rule
    pin = parse_pin((t or {}).get("tags") or "")
    if t is None and h and removed and h in removed:
        v.subject = {**removed[h], "removed_this_batch": True}
    else:
        v.subject = subject_of(h, t, len(wanted))
    season_dir = _show_dir(media_root, path)[1] if media_root else path.parent.name
    v.slot = _slot_arg(args.get("slot")) or pin or name_slot(path, season_dir)
    if args.get("keep_path"):
        v.keeper = {"path": str(args["keep_path"]),
                    "hash": (args.get("keep_hash") or "").lower(),
                    "digest": args.get("keep_digest") or None}
    return pin


def i3_note(wanted: list[dict], entry: dict) -> str:
    return (f"I3：所属种子有 {len(wanted)} 个要下载的文件，整种子作废改为"
            f"只作废这一个条目（{entry['name']}）")


# ------------------------------------------------------------------ drop_torrent
def check_drop(ex, f: Finding, victim: dict) -> Verdict:
    """摘一个种子的记录（`delete_files=False`，盘上一个字节都不动）之前的复核。

    执行器已经复核过"受害者还在、没下完 / 仍是死种；保留方还在、下完了"。这里补上
    **保留方此刻仍替那个共享的文件作保**（I1）：撞车的两个种子指着同一个文件，摘掉没下完的
    那个，是因为完整的那份已经在盘上、由保留方做种。诊断之后保留方若改了名（不再声明那个
    路径）、文件没了或被截断，摘掉受害者就可能让这一集一份都不剩。死种没有保留方：
    执行器另有"没有已下完的成员文件"的复核。
    """
    a = f.action
    v = Verdict(disposition_of(f))
    why = screen(f)
    if why:
        v.gate, v.refused = "evolved", why
        return v
    h = (victim.get("hash") or "").lower()
    v.torrent_hash, v.rule = h, f.rule
    v.subject = subject_of(h, victim, None)
    kh = (a.args.get("keep_hash") or "").lower()
    if not kh:
        return v
    shared = a.args.get("path") or ""
    v.keeper = {"path": shared or None, "hash": kh, "digest": None}
    if kh in _removed(ex):
        return v.refuse("I1", f"保留方的种子 {kh[:8]} 本批次已被摘掉，它不再替那个文件作保")
    if not shared:
        return v
    from .actions import _inside
    p, bad = _inside(shared, Path(ex.cfg.media_root), "path")
    if bad:
        return v.refuse("I1", f"共享的路径不合法：{bad}")
    claims = ex._claims()
    try:
        kt = claims.torrent(kh)
        ke = _entry_at(kt, claims.entries(kh), p) if kt is not None else None
    except ClaimsUnknown as e:
        return v.fail(f"无法确认保留方的占用情况，未做任何改动：{e}")
    if ke is None or ke.get("priority", 1) == 0:
        return v.refuse("I1", f"保留方 {kh[:8]} 已不再声明 {p.name}（诊断之后改过名或设为不下载），"
                              f"受害者也就不再是撞车")
    if not os.path.lexists(p) or p.is_symlink() or not p.is_file():
        return v.refuse("I1", f"保留方声明的 {p.name} 此刻不在盘上（幻影），摘掉受害者这一集就一份都不剩")
    size = p.stat().st_size
    if size < int(ke.get("size") or 0):
        return v.refuse("I1", f"保留方的 {p.name} 盘上只有 {size} 字节，比声明的 {ke['size']} 少"
                              f"（截断或被替换）")
    return v
