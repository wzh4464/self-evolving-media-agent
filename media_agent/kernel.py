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
        """去重键：同一文件同一类问题只报一次。

        兜底到 `(show, summary)` 是必须的，不能让键退化成 `(kind, "")`：
        **有些问题根本没有对应的文件**。抓取器报的"S04E20 可抓取"说的是
        磁盘上还不存在的一集，既没有 path 也没有 torrent_hash，于是全库
        每一条待抓记录都共享同一个键——`run_all` 认为它们是同一个问题，
        除第一条外全部丢弃，而且不留任何日志。

        后果是抓取模型上线后**每轮全库只补得了一集**。看起来完全正常：
        每次 diagnose 都规规矩矩报一条待抓、apply 也成功，只是永远只有一条。
        与"入间同学空转三天"是同一种形态——单条指标全部合格，
        只有把多轮放在一起看才发现它在原地打转。
        """
        return (self.kind,
                self.path or self.torrent_hash or (self.show, self.summary))

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------
# 路径前缀：必须按目录边界比，不能按字符串比
# --------------------------------------------------------------------------
def under(path: str | Path, base: str | Path) -> bool:
    """`path` 是否就是 `base`、或位于 `base` 之下。

    **不要用 `str.startswith(str(base))` 代替它。** 那样比较时
    `/Media/青春猪头` 会"包含" `/Media/青春猪头少年不会梦到兔女郎学姐`——
    两个毫不相干的兄弟目录，只因为一个的名字是另一个的前缀。

    这不是假想的风险，是本库已经发生的损坏：目录改名时用 startswith 圈定
    "受影响的种子"，把兄弟目录的种子也圈了进来，接着 `sp.replace(old, new, 1)`
    把它们 save_path 里的前缀也换掉，于是

        /Media/青春猪头少年不会梦到兔女郎学姐
        → /Media/青春猪头少年不会梦到兔女郎学姐少年不会梦到兔女郎学姐

    每跑一轮叠一层，连叠三轮才被发现——因为每一轮的操作本身都"成功"了。
    """
    p, b = str(path), str(base)
    return p == b or p.startswith(b.rstrip("/") + "/")


def repath(path: str | Path, old_base: str | Path, new_base: str | Path) -> str:
    """把 `path` 从 `old_base` 下重挂到 `new_base` 下。调用前须先过 `under()`。"""
    p, o = str(path), str(old_base).rstrip("/")
    assert under(p, o), f"{p!r} 不在 {o!r} 之下"
    return str(new_base).rstrip("/") + p[len(o):]


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
    def quarantined(self) -> bool:
        """是否已被归置进隔离子目录（`.shorts` / `.extras` / `.other` / …）。

        这些目录以 `.` 开头，Jellyfin/Infuse 不扫描——**东西放进去就等于
        已经处理完了**。整理类规则再对它们提"应该改名""重复集号"，
        就是在要求把已归档的东西重新按正片规范对待。

        实测代价：吊带袜天使 48 个特典短片进了 `.shorts`，规则仍然每轮
        报 48 条 unrenamed + 1 条 duplicate-episode，占全库告警的三分之一。
        """
        return self.season_dir.startswith(".")

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
    is_movie: bool = False        # 见 scan.py 的判定：电影没有"集号"可言

    # 已归置进 `.shorts` / `.extras` / `.other` / `.trailers` 的文件。
    # 单独放一个桶而不是混在 files 里，是因为**"放进隔离区"本身就是处置结果**：
    # 那些目录以 `.` 开头，刮削器不扫描，东西进去就等于处理完了。
    # 混在 files 里会让每一条整理规则都得记得自己跳过它们——漏一条就是
    # 一批复发的告警（吊带袜天使 48 个特典短片曾占全库告警的三分之一）。
    extras_files: list[MediaFile] = field(default_factory=list)


    @property
    def official_title(self) -> str:
        """规范标题优先级：TMDB 本地化标题 > AutoBangumi official_title > 目录名。"""
        if self.tmdb_title:
            return self.tmdb_title
        if self.bangumi and self.bangumi.get("official_title"):
            return self.bangumi["official_title"]
        return self.dir_name



def _episode_keys(show: "Show") -> set:
    """这个目录里出现过的 (季, 集)。用来判断两个目录是不是装着同一批内容。"""
    out = set()
    for f in show.files:
        m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", f.filename)
        if m:
            out.add((int(m.group(1)), int(m.group(2))))
    return out


def tmdb_groups(shows: list) -> dict:
    """按 tmdb_id 把目录分组，并判定每组属于哪种情况。

    多个目录指向同一个 TMDB 条目，有两种截然相反的成因，处置也相反：

    | | 例 | 成因 | 该怎么办 |
    |---|---|---|---|
    | `duplicate` | 3年Z组银八老师（繁/简两个目录，各 12 集） | 同一批内容存了两份 | 合并 |
    | `volumes` | 物语系列（化物语/倾物语/囮物语…共 14 个目录） | TMDB 把多部独立作品收成一个条目 | **原样不动** |

    光看"几个目录共享 tmdb_id"分不开这两者，会把物语系列 14 个目录全改名成
    "物语系列"、挤进同一个目录——那是在毁掉用户按作品分卷的组织方式。

    判据不能用"是否有交集"，也不能用"是否为子集"：物语系列每部都从第 1 集
    开始编号，倾物语的 S01E01–04 **正好是**化物语 S01E01–15 的子集——
    这两条判据都会把它误判成重复（第一版就是这么错的）。

    要看的是**重合的比例**（Jaccard）：同一批内容存两份会几乎完全重合
    （银八老师两个目录都是 S01E01–E12，1.0），不同作品只是编号撞车
    （倾物语 vs 化物语 = 4/15 ≈ 0.27）。空壳目录没有集号可比，
    单独归入 duplicate——那正是"历史遗留的空目录"的形态。
    """
    def _overlap(a: set, b: set) -> float:
        return len(a & b) / len(a | b) if (a or b) else 0.0

    by_id: dict = {}
    for s in shows:
        if s.tmdb_id:
            by_id.setdefault(s.tmdb_id, []).append(s)

    out: dict = {}
    for tid, group in by_id.items():
        host = max(group, key=lambda s: len(s.files))
        if len(group) == 1:
            out[tid] = {"shows": group, "kind": "single", "host": host}
            continue
        keys = {id(s): _episode_keys(s) for s in group}
        hk = keys[id(host)]
        dup = [s for s in group
               if s is not host
               and (not keys[id(s)] or _overlap(keys[id(s)], hk) >= 0.8)]
        out[tid] = {"shows": group, "host": host,
                    "kind": "duplicate" if dup else "volumes",
                    "duplicates": dup}
    return out

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

    def episode_files(self) -> Iterable[MediaFile]:
        """参与剧集整理的文件：排除电影，排除已归置进隔离区的。

        默认用这个而不是 `all_files()`——"这个文件该叫 SxxExx 吗"这个问题
        对电影和特典根本不成立，问了就只能得到噪音。
        """
        for s in self.shows:
            if s.is_movie:
                continue
            for f in s.files:
                if not f.quarantined:
                    yield f


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

        # `unparsable` 的字面意思是"看见了，但归不了类"。要是别的规则**已经**
        # 把它归了类，这句话就不再成立，留着只是把同一个文件报两遍。
        #
        # 出处（2026-08-31）：演进器为 6 个 `.sup` 字幕立了条 classified 规则，
        # 库内问题数从 15 涨到 21——`subtitle_unrenamed: 6` 是新增的，
        # `unparsable: 6` 一条没少。演进循环那边确实收敛了（残留已被解释，
        # 不会再重复提议），但用户看到的是凭空多出 6 条。照这个势头，
        # 每上线一条归类规则，问题总数就永久虚高一截，最后没人再看这个数。
        classified_paths = {f.path for f in out if f.classified and f.path}
        if classified_paths:
            out = [f for f in out
                   if not (f.kind == "unparsable" and f.path in classified_paths)]

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
            if show.is_movie:
                continue          # 演进规则清一色是集号/命名规范，对电影不成立
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
