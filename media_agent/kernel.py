"""插件内核：Finding / Action / Context / Registry / 规则 DSL 解释器。

形态借鉴 deepseek-harness：一切皆插件，capability 与 provider 分离。

**安全边界**：自演进产出的规则是**声明式 DSL**（JSON），由本文件的解释器求值，
绝不 `exec()` 模型生成的 Python。理由见
`.agents/notes/implemented/architecture/2026-08-17-declarative-rule-dsl.md`。
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

Severity = str  # critical | important | minor


# --------------------------------------------------------------------------
# 值对象
# --------------------------------------------------------------------------
@dataclass
class Action:
    """一个可执行的修复动作。"""
    op: str                       # rename | retag | recategorize | relocate | trash | write_nfo | file_prio
    args: dict[str, Any] = field(default_factory=dict)
    reversible: bool = True       # 不可逆动作在全自动模式下也要走隔离区
    note: str = ""


@dataclass
class Finding:
    """一条诊断结论。"""
    rule: str                     # 产出它的规则 id
    kind: str                     # 问题类型
    severity: Severity
    summary: str
    show: str = ""
    path: str = ""
    torrent_hash: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    action: Action | None = None
    # "已归类"：某条规则看懂了这个文件，但不存在值得自动执行的动作。
    # 它与"没有动作"不是一回事——后者可能只是规则没写全。
    # 演进器的残留判据要认这个标记，否则同一批文件会被永远重新提议，
    # 产出一串 v2/v3/residual/leftover 变体（实测累积了 28 条同质规则）。
    classified: bool = False

    def key(self) -> tuple:
        """去重键：同一文件同一类问题只报一次。"""
        return (self.kind, self.path or self.torrent_hash)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class MediaFile:
    """磁盘上的一个文件 + 它在 qBittorrent / AutoBangumi 里的对应关系。"""
    path: Path
    size: int
    show_dir: str                 # Media 下的一级目录名
    season_dir: str               # "Season 1" / "" (直接在 show 根下)
    filename: str
    torrent_hash: str = ""
    torrent_name: str = ""
    torrent_state: str = ""
    torrent_progress: float = 0.0
    torrent_tags: str = ""
    torrent_category: str = ""

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def parent_dir(self) -> str:
        return self.path.parent.name

    @property
    def is_incomplete(self) -> bool:
        """是否尚未下载完成。

        来源是 `torrents/files` 时文件名已经是干净的目标名（没有 `.!qB` 后缀），
        所以主要看种子进度；`.!qB` 后缀只在从磁盘扫到的文件上出现，作为兜底。
        """
        if self.torrent_hash and self.torrent_progress < 1.0:
            return True
        return self.filename.endswith(".!qB")


@dataclass
class Show:
    """一部番：磁盘目录 + AutoBangumi 记录 + TMDB 元数据。"""
    dir_name: str
    dir_path: Path
    bangumi: dict | None = None           # AutoBangumi bangumi 行
    tmdb_id: int | None = None
    tmdb_title: str = ""
    tmdb_seasons: list[dict] = field(default_factory=list)
    files: list[MediaFile] = field(default_factory=list)

    @property
    def official_title(self) -> str:
        """规范标题优先级：TMDB 本地化标题 > AutoBangumi official_title > 目录名。"""
        if self.tmdb_title:
            return self.tmdb_title
        if self.bangumi and self.bangumi.get("official_title"):
            return self.bangumi["official_title"]
        return self.dir_name


@dataclass
class LibraryState:
    """一次扫描得到的完整快照，所有检测器的唯一输入。"""
    shows: list[Show] = field(default_factory=list)
    torrents: list[dict] = field(default_factory=list)
    bangumi_rows: list[dict] = field(default_factory=list)
    rss_rows: list[dict] = field(default_factory=list)
    orphan_torrents: list[dict] = field(default_factory=list)   # content_path 不在 Media 下

    def all_files(self) -> Iterable[MediaFile]:
        for s in self.shows:
            yield from s.files


# --------------------------------------------------------------------------
# Context：承载所有 capability，插件通过它访问外部世界
# --------------------------------------------------------------------------
class Context:
    def __init__(self, config, qbit=None, ab=None, abdb=None,
                 tmdb=None, anilist=None, llm=None, logger=None):
        self.config = config
        self.qbit = qbit
        self.ab = ab
        self.abdb = abdb
        self.tmdb = tmdb
        self.anilist = anilist
        self.llm = llm
        self.log = logger or (lambda *a, **k: None)


# --------------------------------------------------------------------------
# 插件协议
# --------------------------------------------------------------------------
class Detector(Protocol):
    id: str
    kind: str

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        ...


@dataclass
class Registry:
    """插件注册表：内置检测器 + 演进出的 DSL 规则。"""
    detectors: list[Any] = field(default_factory=list)

    def register(self, detector: Any) -> Any:
        self.detectors.append(detector)
        return detector

    def run_all(self, ctx: Context, state: LibraryState) -> list[Finding]:
        seen: set[tuple] = set()
        out: list[Finding] = []
        for d in self.detectors:
            try:
                for f in d.detect(ctx, state):
                    if f.key() in seen:
                        continue      # 先注册的规则优先，避免重复报同一问题
                    seen.add(f.key())
                    out.append(f)
            except Exception as e:      # 单个规则崩溃不能拖垮整轮
                ctx.log(f"[registry] 规则 {getattr(d, 'id', d)} 执行失败: {e}")
        order = {"critical": 0, "important": 1, "minor": 2}
        out.sort(key=lambda f: (order.get(f.severity, 9), f.kind, f.path))
        return out


# --------------------------------------------------------------------------
# 声明式规则 DSL —— 自演进的产物用这个表达，而非生成代码
# --------------------------------------------------------------------------
_FIELD_GETTERS: dict[str, Callable[[MediaFile, Show], Any]] = {
    "filename": lambda f, s: f.filename,
    "ext": lambda f, s: f.ext,
    "parent_dir": lambda f, s: f.parent_dir,
    "show_dir": lambda f, s: f.show_dir,
    "season_dir": lambda f, s: f.season_dir,
    "size": lambda f, s: f.size,
    "path": lambda f, s: str(f.path),
    "torrent_name": lambda f, s: f.torrent_name,
    "torrent_tags": lambda f, s: f.torrent_tags,
    "torrent_state": lambda f, s: f.torrent_state,
    "torrent_category": lambda f, s: f.torrent_category,
    "torrent_progress": lambda f, s: f.torrent_progress,
    "official_title": lambda f, s: s.official_title,
    "has_torrent": lambda f, s: bool(f.torrent_hash),
}

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "regex": lambda v, arg: bool(re.search(arg, str(v), re.IGNORECASE)),
    "not_regex": lambda v, arg: not re.search(arg, str(v), re.IGNORECASE),
    "eq": lambda v, arg: v == arg,
    "ne": lambda v, arg: v != arg,
    "lt": lambda v, arg: _num(v) < _num(arg),
    "gt": lambda v, arg: _num(v) > _num(arg),
    "contains": lambda v, arg: str(arg).lower() in str(v).lower(),
    "glob": lambda v, arg: fnmatch.fnmatch(str(v).lower(), str(arg).lower()),
    "in": lambda v, arg: v in arg,
    "is_true": lambda v, arg: bool(v) is bool(arg),
}


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _eval_clause(clause: dict, f: MediaFile, s: Show) -> bool:
    if "all" in clause:
        return all(_eval_clause(c, f, s) for c in clause["all"])
    if "any" in clause:
        return any(_eval_clause(c, f, s) for c in clause["any"])
    if "not" in clause:
        return not _eval_clause(clause["not"], f, s)
    getter = _FIELD_GETTERS.get(clause.get("field", ""))
    op = _OPS.get(clause.get("op", ""))
    if getter is None or op is None:
        return False
    try:
        return op(getter(f, s), clause.get("value"))
    except Exception:
        return False


@dataclass
class RuleSpec:
    """一条声明式规则。演进器产出它，解释器执行它。"""
    id: str
    kind: str
    severity: Severity
    summary: str
    match: dict
    action: dict | None = None
    source: str = "evolved"       # builtin | evolved
    enabled: bool = True
    # 无动作规则的意图声明：
    #   classified —— 已看懂并归类，不需要自动动作（默认）
    #   unresolved —— 只是标记出来，问题仍悬而未决，欢迎后续规则接手
    # 默认取 classified：一条规则既然写得出精确的匹配条件，就说明它已经
    # 理解了这批文件。若默认成 unresolved，演进器会永远重新提议同一批残留。
    resolution: str = "classified"

    @classmethod
    def from_json(cls, data: dict) -> "RuleSpec":
        return cls(
            id=data["id"],
            kind=data["kind"],
            severity=data.get("severity", "minor"),
            summary=data.get("summary", ""),
            match=data["match"],
            action=data.get("action"),
            source=data.get("source", "evolved"),
            enabled=data.get("enabled", True),
            resolution=data.get("resolution", "classified"),
        )

    def detect(self, ctx: Context, state: LibraryState) -> Iterable[Finding]:
        if not self.enabled:
            return
        for show in state.shows:
            for f in show.files:
                if not _eval_clause(self.match, f, show):
                    continue
                action = None
                if self.action:
                    args = dict(self.action.get("args", {}))
                    args.setdefault("path", str(f.path))
                    if f.torrent_hash:
                        args.setdefault("torrent_hash", f.torrent_hash)
                    action = Action(
                        op=self.action["op"],
                        args=args,
                        reversible=self.action.get("reversible", True),
                        note=self.action.get("note", ""),
                    )
                yield Finding(
                    rule=self.id,
                    kind=self.kind,
                    severity=self.severity,
                    summary=self.summary or f"{self.kind}: {f.filename}",
                    show=show.dir_name,
                    path=str(f.path),
                    torrent_hash=f.torrent_hash,
                    evidence={"matched_by": self.id, "source": self.source,
                              "resolution": self.resolution},
                    action=action,
                    # 有动作的规则天然算已解释；无动作的按其声明的意图判定
                    classified=(action is None and self.resolution == "classified"),
                )


def load_rule_specs(rules_dir: Path) -> list[RuleSpec]:
    """从 .agents/rules/*.json 加载演进出的规则。"""
    specs: list[RuleSpec] = []
    if not rules_dir.exists():
        return specs
    for p in sorted(rules_dir.glob("*.json")):
        try:
            specs.append(RuleSpec.from_json(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return specs
