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
- **I2 不删另一个保留着的种子仍然声明的路径**（`claims.ClaimIndex.check(disk=False)`，
  豁免这次动作自己要处置的那个种子的那个条目；本批次已摘的种子不算）。
- **I3 不为了去掉一个文件整种子作废多文件种子**：所属种子此刻要下载的文件多于一个时，
  **自动降级**成只作废这一个条目（按 `save_path + 条目名` 的完整路径认），并在审计里记一笔；
  只剩这一个时反过来整种子作废（留一个什么都不下的空种子会被 stale-torrent-path 报成死链）。
- **I4 不删封存了集位的文件**：钉了 `ma:SxxEyy`、复核（`meets_requirements`）通过。**探测不可用
  （`probe` 返回 None：超时、出错）一律当作封存**——封存不能因为某一轮 ffprobe 超时就丢；
  唯一的例外是合并发布里同一个种子的兄弟文件：封存由判重选中的那一份（同一个种子、同一个
  钉子）持有，兄弟按用户偏好只留一份。
- 另外：**演进规则产出的删除一律不执行**（critic N5）。第 1 阶段在 `Executor._dispatch` 拦下
  所有演进动作，那道保留作纵深防御；删除的唯一执法点在这里（`screen`）。

（施工中：I2、I3 与演进规则已接线；I1、I4 尚未接线。）

**看不全就拒绝**：qBittorrent 读失败（`ClaimsUnknown`）记 failed「无法确认…占用情况，未做任何
改动」，与占用闸门同口径。

**审计**：`Verdict.audit()` 给每条 trash 记录加一个 `deletion` 字段（新键，旧记录没有它，
读的人要容忍缺失）——purge 要的事实在删除那一刻最全，之后种子可能已经摘了：

    "deletion": {
      "gate": "passed" | "I1" | "I2" | "I4" | "evolved" | "unknown" | "mismatch",
      "disposition": "duplicate" | "extras" | "bundled_version" | "dead_partial" | "manual" | "other",
      "slot": [季, 集] | null,
      "keeper": {"path", "hash", "digest"} | null,
      "subject": {"torrent_hash", "name", "pin", "tags", "category", "torrent_files"},
      "notes": [...]?          # 例如 I3 的自动降级
    }

规则 id 就是记录本身的 `rule`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .claims import PARTIAL, ClaimsUnknown
from .kernel import DSL_ORIGIN, Finding
from .naming import VIDEO_EXTS, parse_episode, parse_pin, season_of_dir

PREFIX = "删除关口："

DISPOSITIONS = ("duplicate", "extras", "bundled_version", "dead_partial", "manual", "other")


def disposition_of(f: Finding) -> str:
    """这次删除属于哪一类处置。**只看产出它的规则**（内置检测器的 id / kind），
    不看动作参数——参数可以由任何人写（手工脚本、演进规则），而处置类别决定了
    I1 的例外（特典 / 半成品可以删掉某集位唯一的文件），不能让参数自己给自己放行。"""
    if f.rule == "duplicate-episode":
        return "bundled_version" if f.kind == "bundled_version" else "duplicate"
    if f.rule == "extras-in-library":
        return "extras"
    if f.rule == "dead-torrent":
        return "dead_partial"
    if f.rule == "manual":
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
        d: dict = {"gate": self.gate, "disposition": self.disposition,
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
    """只凭名字（与所在季目录）认出的集位；认不出返回 None。不看偏移——关口宁可认少。"""
    sn, ep = parse_episode(_base(p).name)
    if ep is None:
        return None
    if sn is None:
        sd = season_of_dir(season_dir or p.parent.name)
        sn = sd if sd is not None else 1
    return sn, ep


def _slot_arg(v) -> tuple[int, int] | None:
    try:
        s, e = v
        return int(s), int(e)
    except (TypeError, ValueError):
        return None


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
        t = claims.torrent(h) if h and h not in ex._removed_torrents else None
        entries = claims.entries(h) if t is not None else []
    except ClaimsUnknown as e:
        return v.fail(f"无法确认种子文件列表与路径占用情况，未做任何改动：{e}")
    wanted = [e for e in entries if e.get("priority", 1) != 0]
    describe(v, f, path, h, t, wanted)

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
    return v


def describe(v: Verdict, f: Finding, path: Path, h: str, t: dict | None,
             wanted: list[dict]) -> tuple[int, int] | None:
    """把审计要的事实填进 `v`（`subject` 与 `slot`），返回此刻种子上的 `ma:` 钉子。"""
    pin = parse_pin((t or {}).get("tags") or "")
    v.subject = {"torrent_hash": h, "name": (t or {}).get("name", ""),
                 "pin": ("S%02dE%02d" % pin) if pin else None,
                 "tags": (t or {}).get("tags", ""), "category": (t or {}).get("category", ""),
                 "torrent_files": len(wanted) if t is not None else None}
    v.slot = _slot_arg(f.action.args.get("slot")) or pin or name_slot(path)
    return pin


def i3_note(wanted: list[dict], entry: dict) -> str:
    return (f"I3：所属种子有 {len(wanted)} 个要下载的文件，整种子作废改为"
            f"只作废这一个条目（{entry['name']}）")
