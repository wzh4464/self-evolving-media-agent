"""动作执行器。

全自动模式下的三层安全网：
1. **隔离区**：删除 = 移入 `state/trash/<日期>/`，可按批次还原；真删只经 `disposal`
   （按处置类别逐个判、先记 `state/purge.jsonl` 再删，见那里的模块文档）。
2. **配额上限**：单轮删除数量/体积超过阈值就整体跳过并告警——防止规则写错批量误删。
3. **审计日志**：每个动作（含失败）落 `state/audit.jsonl`，可回溯可还原。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import disposal
from .claims import ClaimCheck, ClaimIndex, ClaimsUnknown, fold
from .kernel import DSL_ORIGIN, Action, Context, Finding, repath, under
from .naming import parse_episode


# ---------------- 路径参数校验（逆操作 / 删除类动作共用）----------------
#
# 出处：critic N1 / LAT-02。`restore_from_trash` 曾写成
# `Path(u.get("trash_path") or "")`——空串变成 `Path('.')`，它是真值、
# 而且"存在"，于是 `shutil.move('.', dst)`：`os.rename('.')` 报 EINVAL，
# shutil 退回 `copytree(当前目录 → 媒体库)` 再 `rmtree(当前目录)`。
# 2026-09-26 在 scratchpad 复现：cwd 里的 state/audit.jsonl、源码全部被拷进
# 一个叫 `… S01E12.mp4` 的目录，cwd 被清空。生产上 6 条审计记录的
# `trashed_to` 是 null（20260830T132317、20260908T022758 ×3、
# 20260908T143348、20260920T170126），离一次手动 rollback 只差一步——
# 而手动 rollback 的 cwd 正是项目目录（.venv、.env、14GB 隔离区）。
#
# 所以任何从审计记录里读回来的路径，动手前一律过这里：非空、绝对、
# 已规范化（不含 `.`/`..`）、落在允许的根之下且不是根本身。
def _inside(value, root: Path, what: str) -> tuple[Path | None, str | None]:
    """`value` 必须是 `root` 之下（不含 `root` 本身）的规范绝对路径。

    返回 `(规范化后的路径, None)`，或 `(None, 拒绝原因)`。
    """
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    if not s:
        return None, f"{what} 为空"
    if "\0" in s or not os.path.isabs(s):
        return None, f"{what} 不是绝对路径：{s!r}"
    norm = os.path.normpath(s)
    if norm != s.rstrip("/"):
        return None, f"{what} 含 . 或 .. 等未规范化的部分：{s!r}"
    p, r = Path(norm), Path(os.path.normpath(str(root)))
    if p == r or not under(p, r):
        return None, f"{what} 不在允许的根 {r} 之下：{s!r}"
    return p, None


def _bad_name(name, what: str) -> str | None:
    """单个路径分量（文件名 / 目录名）是否合法；合法返回 None。"""
    if not isinstance(name, str) or not name.strip():
        return f"{what} 为空"
    if "/" in name or "\0" in name or name in (".", ".."):
        return f"{what} 不是单个路径分量：{name!r}"
    return None


def _btih(magnet: str) -> str:
    """magnet 里的 v1 infohash（小写十六进制）；32 位 base32 形式先转成十六进制。"""
    m = re.search(r"xt=urn:btih:([0-9A-Za-z]+)", magnet or "")
    if not m:
        return ""
    v = m.group(1)
    if len(v) == 32:
        try:
            return base64.b32decode(v.upper()).hex()
        except (ValueError, TypeError):
            pass
    return v.lower()


def _bad_rel(rel, what: str) -> str | None:
    """种子内相对路径（renameFile 的参数）是否合法；合法返回 None。"""
    if not isinstance(rel, str) or not rel.strip() or "\0" in rel:
        return f"{what} 为空"
    if rel.startswith("/") or ".." in Path(rel).parts:
        return f"{what} 越出种子根目录：{rel!r}"
    return None


_last_run_at: datetime | None = None


def new_run_id() -> str:
    """批次 ID：`YYYYMMDDTHHMMSS.mmm-<pid>`，例如 `20260926T131502.123-48213`。

    以前只精确到秒（`20260926T131502`）。批次 ID 是回退的单元——两个进程在同一秒
    各起一个 Executor，两批改动就会被 `rollback` 当成一批一起撤掉（critic N10）：
    launchd 上 media-agent 与 vpn-watchdog 的 `StartInterval` 同为 21600，将来 1800 秒
    一次的抓取也会每 6 小时与 `run` 对齐一次。

    - **跨进程不撞**：带 pid——同一时刻活着的两个进程 pid 不同，拿不拿运行锁都一样。
    - **进程内不撞**：同一毫秒（或时钟回拨）时顺延到上一个 ID 的下一毫秒，
      进程内严格递增；不忙等，冻结的时钟下也不会卡死。
    - **仍可排序、兼容旧 ID**：前 15 位还是定宽的秒级时间戳，字典序即时间序；
      旧 ID 是新 ID 的前缀形态，`list_runs` 按记录的 `ts` 排序、`rollback --run`
      按整串精确匹配，两种 ID 混在同一份 audit.jsonl 里都照常工作。
    """
    global _last_run_at
    now = datetime.now()
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)
    if _last_run_at is not None and now <= _last_run_at:
        now = _last_run_at + timedelta(milliseconds=1)
    _last_run_at = now
    return f"{now:%Y%m%dT%H%M%S}.{now.microsecond // 1000:03d}-{os.getpid()}"


@dataclass
class ExecReport:
    applied: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    # 非空 = 整批被拒绝执行（qBittorrent 不可用或本轮扫描读不全），值是原因
    refused: str = ""

    def summary(self) -> str:
        if self.refused:
            return f"⛔ 拒绝执行本批次：{self.refused}"
        return f"执行 {len(self.applied)} 项，跳过 {len(self.skipped)} 项，失败 {len(self.failed)} 项"


class Executor:
    def __init__(self, ctx: Context, dry_run: bool = True, run_id: str | None = None):
        self.ctx = ctx
        self.cfg = ctx.config
        self.dry_run = dry_run
        # 一次 apply = 一个 run_id，回退以 run 为单位，这就是"一键回退"的单元
        self.run_id = run_id or new_run_id()
        self.report = ExecReport()
        self._deleted_count = 0
        self._deleted_bytes = 0
        # 本轮 `grab_episode` 写进 sidecar 的集：{show_dir: {(季, 集), …}}。
        # `write_sidecar` 排在最后、且是整份覆盖，而它的 payload 是**诊断阶段**
        # 算出来的快照——不带上这些，本轮刚抓的集会被旧快照盖掉。
        self._grabbed: dict[str, set] = {}
        # 本批次已经处置掉的种子记录与已搬进隔离区的路径。诊断是一次性全量产出的，
        # 同一个输家常常同时挂着 trash（op 5）和 rename（op 6）：种子删了之后
        # 再去 `files()` 就是 404，记成 failed 还会污染 `find_failure_patterns`
        # （testinfra B2，生产 5 次）。后面的动作要先认一认这里。
        self._removed_torrents: set[str] = set()
        self._trashed_paths: set[str] = set()
        # 本批次摘掉的种子在摘的那一刻是谁（`gate.subject_of` 的形状）。同一批后面的隔离
        # （死种的半成品、合集里剩下的条目）问不到它了，审计的 `deletion.subject` 用这里的。
        self._removed_subjects: dict[str, dict] = {}
        # 路径占用索引（`claims.ClaimIndex`），一批次一份、按需建。每做完一次改动
        # （任何不是 skipped 的审计，见 `_audit`）就作废，下一次查询重新问 qBittorrent。
        self._claim_index: ClaimIndex | None = None

    def _claims(self) -> ClaimIndex:
        """本批次共用的占用索引。**任何往媒体库里落一个名字的动作，落笔前都问它**
        （critic N6）。本批次摘掉的种子按引用传进去，qBittorrent 的删除是异步的。"""
        if self._claim_index is None:
            self._claim_index = ClaimIndex(self.ctx.qbit, ignore=self._removed_torrents)
        return self._claim_index

    # ---------------- 审计 ----------------
    def _audit(self, status: str, finding: Finding, action: Action,
               extra: dict | None = None, undo: dict | None = None):
        """记录一条审计。

        `undo` 是**逆操作的完整描述**——有它才谈得上回退。每个真正改动了
        系统状态的动作都必须提供，否则这次改动就是不可逆的，
        `rollback` 会明确报告它跳过了什么，而不是假装回退干净了。
        """
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "status": status,
            "dry_run": self.dry_run,
            "rule": finding.rule,
            "kind": finding.kind,
            "op": action.op,
            "args": action.args,
            "summary": finding.summary,
            **(extra or {}),
        }
        if undo is not None:
            rec["undo"] = undo
        if status != "skipped" and self._claim_index is not None:
            # applied / failed 都可能已经改了东西（failed 也可能是改到一半）：
            # 占用索引作废，后面的动作看到的是这一步之后的状态。
            self._claim_index.invalidate()
        with self.cfg.audit_log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        bucket = {"applied": self.report.applied,
                  "skipped": self.report.skipped,
                  "failed": self.report.failed}[status]
        bucket.append(rec)

    # 动作之间存在安全顺序，与问题严重度无关：
    # 文件改名必须早于目录改名——目录一改，之前算出的文件路径全部失效。
    # 分类/标签不涉及路径，放最前无所谓；删除放最后，让前面的判断都基于完整状态。
    _OP_ORDER = {
        "fix_title_aliases": 0,  # 订阅失效则下游全部无从谈起，最先修
        "repoint_rss": 0,        # 同上：链接指错地方，下游同样无从谈起
        "grab_episode": 0,       # 抓取只加种子、不碰已有文件，与下游动作互不干扰
        "write_sidecar": 10,     # 最后写档案，记录本轮结束后的最终状态
        "relink_torrent": 1,     # 再把失联种子接回来，后续规则才看得到它们
        "drop_torrent": 1,       # 撞车的种子越早摘掉越好：它占着一条路径的
                                 # 所有权，后面的改名/归位都要以此为前提
        "retag": 2, "recategorize": 3, "delete_category": 4,
        "write_nfo": 5,

        # 腾空必须在命名之前 —— 这是改名唯一失败模式的解药。
        #
        # 改名只有一种失败方式：目标名被占。而目标名是 `{标题} SxxExx.ext`，
        # 一个集位按定义只能有一个文件。所以"被占"永远意味着同一集有两个
        # 文件，那是 `duplicate-episode` 的职责，不是命名问题。
        #
        # trash 原先排在最后（op 9），出于"破坏性操作放最后"的直觉。代价是：
        # 同一批次里 rename 先跑、撞名跳过，trash 再把占位的清掉——赢家还留在
        # 原始发布名上，要等**下一轮**（6 小时后）才改名。而"谁先出要谁"必然
        # 产生重复（多个字幕组发同一集是常态），于是这不是边缘情况而是常态：
        # 审计日志里 2657 次 rename/skipped，原因全部是「目标文件名已存在」。
        #
        # 2026-09-04 用户报"最新的入间同学没改名"：E20 有 Sakurato 和 Nix-Raws
        # 两个版本，判重留下 Sakurato，但改名在腾空之前跑，连着两轮都跳过。
        #
        # 三个 trash 来源（duplicate-episode / dead-torrent / extras-in-library）
        # 都不依赖改名先发生，提前执行是安全的；而且 trash 是移入隔离区加逆操作，
        # 不是 rm，"放最后"保护的东西本来就不多。
        "trash": 5,

        "rename": 6,            # 先改文件名（此时目录名还是旧的，路径有效）
        "relocate": 7,
        "rename_show_dir": 8,   # 再改目录名，一次性带走里面所有文件
    }

    # ---------------- 入口 ----------------
    def qbit_blocker(self) -> str:
        """qBittorrent 这一侧是否可信到足以改东西；可信返回空串，否则返回原因。

        本项目的每一个改动都以种子视图为前提（AGENTS.md 第 2、3 条）：
        有种子的文件改名必须走 renameFile，隔离必须先处理种子。视图缺了，
        有种子的文件就会被当成纯本地文件——改名退化成 `mv`、隔离跳过种子。
        LAT-01：2026-09-19 run 20260919T225410 登录超时仍照常执行，把归种子
        d08f05a7 的 `朱音落语 S01E12.mp4` 以 `torrent_hash ""` 移进隔离区，
        下一轮又删了那个种子的记录。所以这里 fail closed：整批拒绝。
        """
        if self.ctx.qbit is None:
            return "qBittorrent 不可用（登录失败或未配置），没有种子视图不能改动任何东西"
        errs = getattr(self.ctx, "qbit_errors", None) or []
        if errs:
            return (f"本轮扫描读 qBittorrent 不完整（{len(errs)} 处失败，首条：{errs[0]}），"
                    f"种子视图有缺口时不能改动任何东西")
        return ""

    def apply(self, findings: list[Finding]) -> ExecReport:
        blocked = self.qbit_blocker()
        if blocked:
            # 不逐条写审计：这些动作一个都没有尝试。拒绝本身由调用方大声报告
            # （cli 打印到 stdout/stderr 并以 EXIT_DEGRADED 退出）。
            self.report.refused = blocked
            return self.report
        ordered = sorted(
            (f for f in findings if f.action),
            key=lambda f: (self._OP_ORDER.get(f.action.op, 99), f.show, f.path),
        )
        for f in ordered:
            try:
                self._dispatch(f, f.action)
            except Exception as e:
                self._audit("failed", f, f.action, {"error": f"{type(e).__name__}: {e}"})
        return self.report

    def _dispatch(self, f: Finding, a: Action) -> None:
        if (f.evidence or {}).get("origin") == DSL_ORIGIN:
            # critic N5：演进规则由 LLM 提议、影子验证只在上线那一刻跑一次，此后
            # 不再复核；它的动作参数也由模型选（甚至可以自带 path 覆盖）。一条
            # `retag ma:SxxExx` 就能决定改名目标与判重去留——这是未经人审的
            # "LLM → 改名 / 删除"通路。在人工确认放行的流程落地之前，一律不执行。
            # 生产上 30 条演进规则的 action 全是 null，这道闸今天行为中立。
            self._audit("skipped", f, a, {
                "reason": "演进规则未经人工确认，不自动执行其动作",
                "rule_source": f.evidence.get("source", "")})
            return
        handler = getattr(self, f"_op_{a.op}", None)
        if handler is None:
            self._audit("skipped", f, a, {"reason": f"未知动作 {a.op}"})
            return
        handler(f, a)

    # ---------------- 各类动作 ----------------
    def _op_rename(self, f: Finding, a: Action) -> None:
        path = Path(a.args["path"])
        new_name = (a.args.get("new_name") or "").strip()
        # 纵深防御：空目标名会把文件改名成父目录。验证层已拦一道，这里再拦一道。
        if not new_name or "/" in new_name or new_name in (".", ".."):
            self._audit("skipped", f, a, {"reason": f"非法目标文件名 {new_name!r}"})
            return
        target = path.parent / new_name
        h = a.args.get("torrent_hash")

        # 先认"本批次已经处置掉的"，再谈别的（testinfra B2）。以前这里先查
        # 目标名被占：输家被搬走、赢家刚改好名，于是报一条误导的「集位被占」
        # （20260924T173911 / T234117）；有种子的输家则去问已删种子的 `files()`，
        # 404 记成 failed（生产 5 次），还被当成"规则本身有问题"。
        if h and h in self._removed_torrents:
            self._audit("skipped", f, a, {
                "reason": "所属种子已在本批次被移除（判重作废 / 死种 / 撞车），不再改名"})
            return
        if str(path) in self._trashed_paths:
            self._audit("skipped", f, a, {"reason": "文件本批次已移入隔离区，不再改名"})
            return
        if not h and not os.path.lexists(path):
            self._audit("skipped", f, a, {"reason": "文件已不在原位，不再改名"})
            return

        if h and not self.ctx.qbit:
            # AGENTS.md 第 3 条：有种子的文件绝不走文件系统改名。以前 qBit 不在时
            # 这里直接落到下面的 `path.rename`（apply 的总闸之外的纵深防御）。
            self._audit("skipped", f, a, {
                "reason": "有种子的文件，但 qBittorrent 不可用：拒绝绕过它改名"})
            return
        # 目标名此刻归谁——盘上的 X / X.!qB、别的种子的条目，大小写与 Unicode 规范化
        # 不敏感（claims 模块文档）。以前只看 `target.exists()` 加一个逐字符比较的
        # `_claimants`：别人的孤儿 `X.!qB`、只差大小写的声明都看不见；自己只改大小写
        # 反被 APFS 上的 `exists()` 当成集位被占。
        chk = self._claims().check(target, own_hash=h or "", own_path=path)
        if chk.unknown:
            self._audit("failed", f, a, {
                "error": f"无法确认目标路径的占用情况，未做任何改动：{chk.unknown}",
                "claims": chk.audit()})
            return
        if chk.claimants:
            self._audit("skipped", f, a, self._occupied(target, chk))
            return
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return

        via = "filesystem"
        if h:
            # 有种子的一律走 qBittorrent API。找不到对应条目就报失败，
            # **绝不退化成文件系统改名**——那会让种子路径失效、做种中断。
            gone = {"reason": "所属种子已不在 qBittorrent 里，不再改名"}
            t = self._claims().torrent(h)
            if t is None:
                # 诊断之后、本批次之外被删的（手动删种等）：状态变了，不是规则错
                self._audit("skipped", f, a, gone)
                return
            try:
                entries = self.ctx.qbit.files(h)
            except Exception as e:
                if "404" in str(e):
                    self._audit("skipped", f, a, gone)
                    return
                raise
            # 按完整路径（save_path + 条目名）认，不按文件名：合集里不同子目录下
            # 同名的文件（`a/E05.mkv`、`b/E05.mkv`）按文件名会认错、改掉另一个。
            entry = self._entry_at(t, entries, path)
            if entry is None:
                self._audit("failed", f, a,
                            {"error": "种子文件列表里找不到该文件，拒绝绕过 qBittorrent 改名"})
                return
            old_rel = entry["name"]
            # 本轮已经被作废（设为不下载、移进隔离区）的，就别再改名了。
            # 诊断是一次性全量产出的：合并发布种子里的两个文件都会被提「改成
            # 规范名」，而其中一个同时被提「只作废这一个文件」。作废排在改名
            # 之前，等轮到它改名时文件已不在盘上，qBittorrent 却仍列着这个条目
            # （优先级 0）——照改会把一个不存在的文件映射到规范名上。
            if entry.get("priority", 1) == 0:
                self._audit("skipped", f, a,
                            {"reason": "该文件已设为不下载（本轮已作废）", "at": old_rel})
                return
            # 种子说这个文件已下完，盘上（连 `.!qB` 都）没有：它是幻影，改名只会把
            # 一个不存在的文件映射到集位名上，此后它就"宣称"那个集位。
            # 下载中的文件盘上本来就可能还没有——那种照改，是支持的功能。
            if (entry.get("progress", 0) >= 1 and not os.path.lexists(path)
                    and not os.path.lexists(str(path) + ".!qB")):
                self._audit("skipped", f, a, {
                    "reason": "种子说已下完、文件却不在原位（幻影或被挪走），不改名",
                    "at": old_rel})
                return
            new_rel = str(Path(old_rel).parent / new_name) if "/" in old_rel else new_name
            self.ctx.qbit.rename_file(h, old_rel, new_rel)
            via = "qbittorrent"
        else:
            # 确认无种子关联才允许文件系统改名（纯本地文件，无从同步）
            path.rename(target)
        self._audit("applied", f, a, {"new_path": str(target), "via": via},
                    undo={"op": "rename", "path": str(target),
                          "new_name": path.name, "torrent_hash": h or ""})

    # macOS 会在共享卷上撒这些元数据文件，它们不算"内容"，
    # 判断目录是否为空、是否值得搬运时都应忽略
    _JUNK_PREFIXES = ("._",)
    _JUNK_NAMES = {".DS_Store", "Thumbs.db", ".localized"}

    @classmethod
    def _is_junk(cls, name: str) -> bool:
        return name in cls._JUNK_NAMES or name.startswith(cls._JUNK_PREFIXES)

    def _merge_tree(self, old: Path, new: Path,
                    skip: Collection[str] = frozenset()) -> tuple[int, int]:
        """把 old 目录树递归合并进 new，逐**文件**移动并保持相对结构。

        必须递归：早先的实现只遍历顶层，遇到 `Season 1` 这种子目录时，
        若 new 下已存在同名子目录就整个跳过，导致里面的文件全部滞留在旧目录，
        造成新旧两个目录并存的分裂状态（实测 19 个目录、50 个文件中招）。

        `skip` 里的路径一个都不碰——调用方传入**此刻有种子声明**的路径，按
        `claims.fold` 折叠过（见 `ClaimIndex.claims_under`；盘上的名字与条目只差大小写
        时也认得出）。文件系统搬走种子的文件就是 AGENTS.md 第 3 条说的死链；它们要么
        已由 setLocation 交给 qBittorrent 搬（异步，可能还没搬完），要么根本不该由这里搬。

        返回 (已移动文件数, 因目标已存在而滞留的文件数)。
        """
        moved = stranded = 0
        if not old.exists():
            return 0, 0
        new.mkdir(parents=True, exist_ok=True)

        for src in sorted(old.rglob("*")):
            if not src.is_file() or fold(src) in skip:
                continue
            rel = src.relative_to(old)
            if self._is_junk(src.name):
                src.unlink(missing_ok=True)     # 元数据垃圾直接丢弃，不搬
                continue
            dest = new / rel
            if dest.exists():
                stranded += 1                   # 同名文件已在，留着让去重规则处理
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved += 1

        # 自底向上清理空目录（此时只剩空壳）
        for d in sorted((p for p in old.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            old.rmdir()
        except OSError:
            pass
        return moved, stranded

    @staticmethod
    def _occupied(target: Path, chk: ClaimCheck) -> dict:
        """「集位被占」的跳过记录。走到这里说明腾空没能发生。"""
        extra = {"occupant": target.name,
                 "claimants": [c.as_dict() for c in chk.claimants]}
        if chk.in_qbit:
            # 另一个活种子声明着它（幻影、还没落盘的下载、只差大小写的名字）。改过去
            # 就是两个种子宣称同一个文件——scan 每个路径只出一条、colliding-torrent
            # 不管两个都 100% 的，此后每轮都看不见；那个种子一旦 recheck，还会把这份
            # 文件当成它自己的来校验、覆盖（2026-09-26 审查复现）。
            extra["reason"] = ("集位被占：目标路径仍被另一个种子声明（"
                               + "、".join(f"{c.hash[:8]} {c.name[:40]}"
                                          for c in chk.in_qbit[:2])
                               + "），改过去就是两个种子争同一个文件")
            extra["hint"] = ("多半是幻影（种子说已下完、盘上没有）或还在下的另一个版本；"
                             "判重会摘掉幻影输家，其余情形需要人工核对")
        elif any(c.partial for c in chk.on_disk):
            # 盘上只有别人的 `X.!qB`：下载中的文件改名是 `A.!qB → X.!qB`，撞上它就是
            # 覆盖或 "File exists"；已下完的改过去，旁边还躺着一份谁也不认的半成品。
            extra["reason"] = ("集位被占：盘上已有别人的半成品 "
                               + "、".join(c.path.rsplit("/", 1)[-1]
                                          for c in chk.on_disk if c.partial)
                               + "，改过去会与它撞在一起")
            extra["hint"] = ("没有种子认领的是孤儿半成品（种子已摘，死种处置会留下），"
                             "需要人工清理；有种子的等它下完交给 duplicate-episode")
        else:
            # 同一集位有两个文件，而 duplicate-episode 这一轮没有（或不能）判出赢家。
            # 不是命名问题，别当命名问题报。
            extra["reason"] = "集位被占：目标名已被另一个文件占用，且本轮没有腾空"
            extra["hint"] = ("同一集有多个版本，等 duplicate-episode 判出取舍；"
                             "若它也判不了（画质无法比较等），需要人工介入")
        return extra

    @staticmethod
    def _entry_at(t: dict, entries: list[dict], abs_path: Path) -> dict | None:
        """种子 `t` 的条目里，`save_path + 条目名` 恰好是 `abs_path` 的那一条。

        按完整路径认，**不按文件名**：合集里不同子目录下同名的文件（`a/E05.mkv`、
        `b/E05.mkv`）按文件名会认到第一个。scan 给 MediaFile 的路径就是
        `save_path.rstrip("/") + "/" + 条目名`，逆改名记下的路径也由它推出，逐字相等。
        """
        sp = Path((t.get("save_path") or "").rstrip("/") or "/")
        return next((e for e in entries if sp / e["name"] == abs_path), None)

    def _torrent_rel_path(self, torrent_hash: str, abs_path: Path) -> str | None:
        """qBittorrent 的 renameFile 用的是种子内相对路径，不是绝对路径。

        种子已不在、或它的条目里没有这个路径，返回 None。
        """
        t = self._claims().torrent(torrent_hash)
        if t is None:
            return None
        e = self._entry_at(t, self.ctx.qbit.files(torrent_hash), abs_path)
        return e["name"] if e else None

    def _op_retag(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        self.ctx.qbit.add_tags([a.args["torrent_hash"]], a.args["tags"])
        self._audit("applied", f, a,
                    undo={"op": "remove_tags", "torrent_hash": a.args["torrent_hash"],
                          "tags": a.args["tags"]})

    def _op_recategorize(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        # 旧分类要在改之前记下来，否则无从回退
        prev = f.evidence.get("current", "")
        self.ctx.qbit.set_category([a.args["torrent_hash"]], a.args["category"])
        self._audit("applied", f, a,
                    undo={"op": "recategorize", "torrent_hash": a.args["torrent_hash"],
                          "category": prev})

    def _op_write_sidecar(self, f: Finding, a: Action) -> None:
        """写入每部番的采集档案。纯写自有文件，不碰媒体内容。"""
        from . import sidecar as sc_mod
        show_dir = Path(a.args["show_dir"])
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        prev = sc_mod.path_for(show_dir)
        prev_content = prev.read_text(encoding="utf-8") if prev.exists() else None
        payload = a.args["payload"]
        known = {k for k in sc_mod.Sidecar.__dataclass_fields__}
        sc = sc_mod.Sidecar(**{k: v for k, v in payload.items() if k in known})

        # payload 是**诊断阶段**的快照，而抓取（op 0）排在本动作（op 10）之前，
        # 已经往磁盘上的 sidecar 写过新集。整份覆盖会把它抹掉：实测 2026-09-26
        # 「躲在超市后门抽烟的两人」S01E12 文件都落盘了，`have` 还停在 11，
        # 于是下一轮把同一集又抓一遍（靠 qBittorrent 的 infohash 去重才没真重下）。
        #
        # 只并回**本轮自己抓的**那几集，不并磁盘上的旧值——sidecar-sync 的职责
        # 之一正是把已经删掉的集从 `have` 里摘掉，无脑求并集会让它再也摘不掉。
        for season, ep in self._grabbed.get(str(show_dir), ()):
            info = sc.seasons.setdefault(str(season), {})
            info["have"] = sorted(set(info.get("have") or []) | {ep})

        # 别名是只增不减的（`add_alias` 的语义），抓取时记下的发布名同样要保住。
        if prev_content:
            try:
                import json as _json
                for al in (_json.loads(prev_content).get("aliases") or []):
                    sc.add_alias(al)
            except (ValueError, AttributeError):
                pass

        sc_mod.save(show_dir, sc)
        self._audit("applied", f, a,
                    undo={"op": "restore_sidecar", "show_dir": str(show_dir),
                          "prev": prev_content})

    def _wait_ab_ready(self, timeout: float = 45.0) -> bool:
        """等 AutoBangumi 重新起来再调它的接口。

        `abdb.write()` 是"停容器 → 改库 → 起容器"，而容器起来后 uvicorn
        还要几秒才开始监听。紧接着调 refresh 会撞上 `Connection reset by
        peer`——**库改对了、刷新却没做**，外部看起来像"改了不生效"，
        很容易误判成规则本身有问题。实测踩过。
        """
        if not self.ctx.ab:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.ctx.ab.all_bangumi()
                return True
            except Exception:
                time.sleep(2)
        return False

    def _op_fix_title_aliases(self, f: Finding, a: Action) -> None:
        """修复订阅的标题匹配，并解除因此卡住的重抓阻塞。

        **三步顺序不能反**，这是实测踩出来的：
        1. 先写 `title_aliases` —— 让匹配重新生效
        2. 再清掉该订阅下"已登记但 qBittorrent 里不存在"的 torrent 记录 ——
           `pull_rss` 只处理 `check_new()` 筛出的新条目，已登记的永不重评
        3. 最后刷新 RSS

        顺序反了（先清记录再改别名）的话，AutoBangumi 会用**仍然失效**的匹配规则
        把这些条目重新登记一遍，于是它们又变成"不新"，白清一轮。
        """
        bid = a.args["bangumi_id"]
        aliases = a.args["aliases"]
        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_set_aliases": aliases})
            return

        rows = self.ctx.abdb.query(
            "SELECT title_aliases FROM bangumi WHERE id=?", (bid,))
        prev = rows[0]["title_aliases"] if rows else None

        # 步骤 1+2 合并成一次停容器写库，减少中断
        stmts = [("UPDATE bangumi SET title_aliases=? WHERE id=?",
                  (json.dumps(aliases, ensure_ascii=False), bid))]

        b = next((x for x in self.ctx.abdb.bangumi() if x["id"] == bid), None)
        cleared = 0
        if b:
            keys = [k for k in (b.get("title_raw"), b.get("official_title")) if k]
            qbit_names = {t["name"] for t in self.ctx.qbit.torrents()}
            for r in self.ctx.abdb.query(
                    "SELECT id, bangumi_id, name FROM torrent"):
                if (r["bangumi_id"] == bid
                        or any(k in (r["name"] or "") for k in keys)):
                    if (r["name"] or "") not in qbit_names:
                        stmts.append(("DELETE FROM torrent WHERE id=?", (r["id"],)))
                        cleared += 1

        self.ctx.abdb.write(stmts)

        # 步骤 3：让 AutoBangumi 重新拉一遍（必须等它起来，见 _wait_ab_ready）
        refreshed = False
        if self.ctx.ab and self._wait_ab_ready():
            try:
                self.ctx.ab.refresh_all()
                refreshed = True
            except Exception as e:
                self.ctx.log(f"[fix_title_aliases] 刷新失败: {e}")

        self._audit("applied", f, a,
                    {"aliases": aliases, "cleared_stuck_records": cleared,
                     "refreshed": refreshed},
                    undo={"op": "restore_title_aliases",
                          "bangumi_id": bid, "prev": prev})

    def _op_repoint_rss(self, f: Finding, a: Action) -> None:
        """把订阅从"搜索式 RSS"改到"番组+字幕组式 RSS"，并补上新发布名的别名。

        搜索式链接（`RSS/Search?searchstr=ANi+You+and+I+Are+Polar+Opposites+...`）
        把字幕组当时的发布名写死进了查询，对方一改名就再也搜不到新集。
        番组式链接（`RSS/Bangumi?bangumiId=..&subgroupid=..`）绑的是 Mikan 的
        番组与字幕组 id，改名不受影响。

        两件事必须一起做：**只换链接不补别名等于白换**——新集是拉回来了，
        但 `match_torrent` 用的还是旧 `title_raw`，照样认领不了。实测踩过。

        `rssitem` 表也要跟着改：AutoBangumi 的定时刷新遍历的是那张表，
        只改 `bangumi.rss_link` 的话手动刷新有效、定时刷新依旧走老链接。

        **不清 torrent 记录**——这是与 `fix_title_aliases` 的关键差别。
        那套清理是给"条目已登记但没下下来"准备的；这里旧集本来就下好了，
        而新集从未登记、天然是新条目。而且 torrent 表存的是 RSS 原始标题，
        与 qBit 里被 AutoBangumi 改过的名字对不上，"不在 qBit"会全部误判为真，
        照搬会把已下好的记录一并抹掉。
        """
        bid = a.args["bangumi_id"]
        new_url = a.args["rss_link"]
        aliases = a.args.get("aliases") or []

        rows = self.ctx.abdb.query(
            "SELECT rss_link, title_aliases FROM bangumi WHERE id=?", (bid,))
        prev_url = rows[0]["rss_link"] if rows else ""
        prev_aliases = rows[0]["title_aliases"] if rows else None

        # 指向同一条旧链接的 rssitem 一并改；先取出来，回退时要按 id 还原
        item_ids = [r["id"] for r in self.ctx.abdb.rss_items()
                    if (r.get("url") or "") == prev_url]

        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_set_rss": new_url,
                         "would_set_aliases": aliases,
                         "would_update_rssitem": item_ids})
            return

        stmts = [("UPDATE bangumi SET rss_link=?, title_aliases=? WHERE id=?",
                  (new_url, json.dumps(aliases, ensure_ascii=False), bid))]
        stmts += [("UPDATE rssitem SET url=? WHERE id=?", (new_url, i))
                  for i in item_ids]
        self.ctx.abdb.write(stmts)

        refreshed = False
        if self.ctx.ab and self._wait_ab_ready():
            try:
                self.ctx.ab.refresh_all()
                refreshed = True
            except Exception as e:
                self.ctx.log(f"[repoint_rss] 刷新失败: {e}")

        self._audit("applied", f, a,
                    {"rss_link": new_url, "aliases": aliases,
                     "updated_rssitem": item_ids, "refreshed": refreshed},
                    undo={"op": "restore_rss_link", "bangumi_id": bid,
                          "prev_rss_link": prev_url,
                          "prev_aliases": prev_aliases,
                          "rssitem_ids": item_ids})

    def _op_grab_episode(self, f: Finding, a: Action) -> None:
        """抓取某一集：下 .torrent、加进 qBittorrent、把集号写进 sidecar。

        `have` 清单是"先到先得"模型的判重依据——不锁字幕组，靠集号去重。
        所以**必须在加种成功后立刻写**，否则下一轮会把同一集再抓一遍。

        qBittorrent 对已存在的 infohash 返回 409。那不是失败，是"已经有了"，
        同样要把集号记进 have（本 session 在 Re:Zero 上把 409 误判成失败过一次，
        白查了半天）。
        """
        import urllib.request

        from . import sidecar as sc_mod

        url = a.args["url"]
        show_dir = Path(a.args["show_dir"])
        season = int(a.args["season"])
        ep = int(a.args["episode"])
        bid = a.args.get("bangumi_id")
        save_path = show_dir / f"Season {season}"

        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_save_to": str(save_path)})
            return

        req = urllib.request.Request(url, headers={"User-Agent": "media-agent/0.1"})
        blob = urllib.request.urlopen(req, timeout=60).read()

        # 把抓取时算出的规范集号钉在标签上。抓取器是按番组页 + 播出日期定位的，
        # 到了改名阶段这个信息就只剩发布名里那个数字——而它可能是分季编号。
        tags = [f"ma:S{season:02d}E{ep:02d}"]

        # `ab:` 标签把改名权交给 AutoBangumi。只有当 AB 会算出同一个集号时
        # 才给——它的 episode_offset 是整条订阅一个值，表达不了"同一目录里
        # 不同来源季用不同偏移"。给错了的代价是真实文件被覆盖：2026-08-31
        # `[Fyy Raws] ... 3rd Season - 08` 被 AB 改成 S01E08，撞上 2016 年的
        # 第 8 集，随后判重规则把 1.31GB 的原片清进了隔离区。
        _, raw_ep = parse_episode(a.args.get("title") or "")
        if bid and raw_ep == ep:
            tags.append(f"ab:{bid}")

        # 分类就是所有权边界。AutoBangumi 的改名线程扫的是
        # `torrents_info(category="Bangumi", status_filter="completed")`——
        # 标签它压根不看（`tag=None`），所以摘 `ab:` 拦不住它，改分类才能。
        #
        # 自己抓的种子直接落进「每部番一个分类」，AB 从一开始就查不到它，
        # 改名全程由本项目负责。2026-08-31 那次 Re:Zero E58 被改成 S01E08、
        # 撞掉 2016 年真正第 8 集的事故，起点就是这里写死的 `"Bangumi"`——
        # 等于把自己下的种子拱手放进了 AB 的地盘。
        cat = a.args.get("category") or "Bangumi"
        try:
            added = self.ctx.qbit.add_torrent(
                blob, save_path=str(save_path), category=cat, tags=",".join(tags))
        except Exception as e:
            self._audit("failed", f, a, {"error": f"加种子失败: {e}"})
            return
        already = not added
        self._claims().invalidate()          # 多了一个种子：占用索引要重新问

        # 发布方的分季编号与库内连续编号不一致时，改写 qBittorrent 里的**种子名**。
        #
        # AutoBangumi 的改名线程按分类扫种子（摘掉 `ab:` 标签也拦不住它），
        # 而且解析的是种子名而不是文件名。我们把文件改成 `S01E58.mkv`，它下一轮
        # 又按种子名里的 `3rd Season - 08` 改回 `S01E08.mkv`——2026-08-31 就这么
        # 来回拉锯，中间那次把 2016 年真正的第 8 集当重复清进了隔离区。
        #
        # 与其屏蔽 AB，不如让它算出同一个答案：种子名里的分季集号换成连续集号，
        # 两边就永久一致了。种子名只是本地显示名，不影响 infohash 和做种。
        if raw_ep is not None and raw_ep != ep:
            self._retitle_torrent(blob, a.args.get("title") or "", raw_ep, ep)

        # 加种成功就立刻改名，别等下一轮。
        #
        # 分类改成剧名之后，AutoBangumi 再也看不到这个种子（它只扫
        # `category="Bangumi"`），改名责任全在本项目。而本项目一轮 run 是
        # "先全量诊断、再统一执行"——抓取发生在执行阶段，`unrenamed-file`
        # 的检测早就跑完了，于是新抓的集要等**下一轮**（6 小时后）才改名。
        #
        # 2026-09-03 用户报"最新三集刮削失败"：文件躺在库里，名字还是
        # `[Nekomoe kissaten][20 Seiki Denki Mokuroku][09][1080p][JPSC].mp4`，
        # Jellyfin 认不出来。以前落在 Bangumi 分类时 AB 60 秒就改好了，
        # 换成自己管之后反而慢了六小时——这是所有权改动带来的回归。
        #
        # 集号在这里是确定的（就是钉进 `ma:` 标签的那个），不需要再解析文件名。
        #
        # 集位被占就不改（占用闸门，见 `grabber.rename_single_video`）：留在发布名上、
        # 钉子留着，下完后判重封存它、清走占位的，同一轮再改名。结局写进审计。
        renamed = self._rename_grabbed(blob, a.args.get("official_title") or cat, season, ep)

        # 写 sidecar：加进 have，并把这个发布名记成别名（下次匹配用得上）
        sc = sc_mod.load(show_dir)
        info = sc.seasons.setdefault(str(season), {})
        have = sorted(set(info.get("have") or []) | {ep})
        info["have"] = have
        title = a.args.get("title", "")
        for part in re.split(r"\s*/\s*", title):
            part = part.strip()
            if 4 <= len(part) <= 60 and not part.startswith("["):
                sc.add_alias(part)
        sc_mod.save(show_dir, sc)
        self._grabbed.setdefault(str(show_dir), set()).add((season, ep))

        self._audit("applied", f, a,
                    {"already_present": already,
                     "save_path": str(save_path),
                     "have_after": have,
                     "rename": renamed},
                    undo={"op": "ungrab_episode", "show_dir": str(show_dir),
                          "season": season, "episode": ep,
                          "title": title})

    @staticmethod
    def _infohash_v1(blob: bytes) -> str | None:
        """从 .torrent 里算 v1 infohash（info 字典的 SHA-1）。

        只为定位刚加进去的那个种子。qBittorrent 对重复 infohash 返回 409，
        此时按名字反查是找不到的（库里那个可能已被改过名），所以要算哈希。
        """
        import hashlib

        def parse(i: int):
            c = blob[i:i + 1]
            if c == b"d":
                i += 1
                start = i
                while blob[i:i + 1] != b"e":
                    _, i = parse(i)          # key
                    _, i = parse(i)          # value
                return (start, i), i + 1
            if c == b"l":
                i += 1
                while blob[i:i + 1] != b"e":
                    _, i = parse(i)
                return None, i + 1
            if c == b"i":
                j = blob.index(b"e", i)
                return None, j + 1
            j = blob.index(b":", i)
            n = int(blob[i:j])
            return blob[j + 1:j + 1 + n], j + 1 + n

        try:
            i = blob.index(b"4:infod") + len(b"4:info")
            _, end = parse(i)
            return hashlib.sha1(blob[i:end]).hexdigest()
        except Exception:
            return None

    def _retitle_torrent(self, blob: bytes, title: str, raw_ep: int, ep: int) -> None:
        """把种子名里的分季集号改写成库内连续集号。失败只记日志，不影响抓取。"""
        h = self._infohash_v1(blob)
        if not h:
            return
        new = re.sub(
            r"(\d{1,2})\s*(?:st|nd|rd|th)\s+season\s*[-–]\s*0*%d\b(?:\s+REV)?" % raw_ep,
            "- %d" % ep, title, flags=re.I)
        if new == title:                     # 没有"第 N 季"字样，就只换数字
            new = re.sub(r"(?<![\d])0*%d(?![\d])" % raw_ep, str(ep), title, count=1)
        if new == title:
            return
        try:
            self.ctx.qbit.rename_torrent(h, new)
        except Exception:
            pass

    def _rename_grabbed(self, blob: bytes, title: str, season: int, ep: int) -> dict:
        """把刚加进去的种子里那个正片文件改成规范名，返回写进审计的结局
        （`RenameOutcome.audit()` 的形状：`{"renamed": 新条目名|None, "skipped"?, "claims"?}`）。

        只处理"恰好一个视频文件"的种子；合集或带特典的交给 `unrenamed-file`
        按文件逐个判断，这里不猜。没改成不让抓取算失败——下一轮的改名规则会兜底；
        但**为什么没改**要进审计与日志，以前全被吞进一行日志、审计里什么都没有。
        """
        from .grabber import rename_single_video, wait_metadata

        h = self._infohash_v1(blob)
        if not h:
            return {"renamed": None, "skipped": "算不出 v1 infohash（纯 v2 种子？），交给 unrenamed-file"}
        stem = "%s S%02dE%02d" % (title, season, ep)
        try:
            files = wait_metadata(self.ctx.qbit, h, timeout=10.0)
            if not files:
                # 元数据还没到，交给 unrenamed-file 兜底
                return {"renamed": None, "skipped": "元数据 10 秒内未到，交给 unrenamed-file"}
            out = rename_single_video(self.ctx.qbit, h, stem, files, claims=self._claims())
        except Exception as e:
            self.ctx.log(f"[grab] 加种后改名失败（下一轮会补）: {e}")
            return {"renamed": None, "skipped": f"改名出错：{type(e).__name__}: {e}"}
        if out.blocked:
            self.ctx.log(f"[grab] 加种后不改名：{stem} {out.skipped}（{out.check.describe()}）；"
                         f"留在发布名上、保留 ma: 钉子，下完后交给判重封存与改名规则收敛")
        return out.audit()

    def _op_drop_torrent(self, f: Finding, a: Action) -> None:
        """把种子记录从 qBittorrent 摘掉，**文件一个字节都不动**。

        用在"两个种子抢同一个文件"的场合：完整的那份数据已经在磁盘上了，
        由另一个种子继续做种，被摘掉的这个只是个下不完的记录。

        执行前重新查一次 qBittorrent，而不是信扫描时的快照：
        检测到执行之间可能过了几分钟，卡住的那个也许已经自己完成了。
        对不上就跳过——这是删除类操作，宁可白跑一趟。

        `dead=True` 是死种（dead-torrent）的用法：没有保留方，改为复核它此刻
        **仍然是死的**，且没有已下完的成员文件（那是可播的正片，还在做种）。
        """
        from . import gate
        refused = gate.screen(f)
        if refused:
            self._audit("skipped", f, a, {
                "reason": refused,
                "deletion": {"gate": "evolved", "disposition": gate.disposition_of(f),
                             "rule": f.rule}})
            return
        h = a.args["torrent_hash"]
        keep_hash = a.args.get("keep_hash", "")
        dead = bool(a.args.get("dead"))
        if not h:
            self._audit("skipped", f, a, {"reason": "缺 torrent_hash"})
            return

        cur = {t["hash"]: t for t in (self.ctx.qbit.torrents() if self.ctx.qbit else [])}
        victim, keeper = cur.get(h), cur.get(keep_hash)
        if victim is None:
            self._audit("skipped", f, a, {"reason": "种子已不在 qBittorrent 里"})
            return
        if victim.get("progress", 0) >= 1.0:
            self._audit("skipped", f, a,
                        {"reason": "它自己已经下完了，不再是撞车的受害者"})
            return
        if dead:
            from .plugins.builtin import is_dead_now
            if not is_dead_now(victim):
                self._audit("skipped", f, a,
                            {"reason": "它又有做种或可用副本了，不再是死种"})
                return
            done = [e["name"] for e in self.ctx.qbit.files(h)
                    if e.get("priority", 1) != 0 and e.get("progress", 0) >= 1]
            if done:
                self._audit("skipped", f, a,
                            {"reason": f"死种里有 {len(done)} 个已下完的文件，不自动摘"})
                return
        elif keeper is None or keeper.get("progress", 0) < 1.0:
            self._audit("skipped", f, a,
                        {"reason": "作为保留方的那个种子已不完整，删了会丢内容"})
            return

        # 删除关口：保留方此刻仍替那个共享的文件作保（I1）——还声明着它、它在盘上、没被截断
        v = gate.check_drop(self, f, victim)
        extra = v.audit()
        if v.failed:
            self._audit("failed", f, a, {"error": v.failed, **extra})
            return
        if v.refused:
            self._audit("skipped", f, a, {"reason": v.refused, **extra})
            return

        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_drop": victim.get("name", ""),
                         "keep": (keeper or {}).get("name", ""), **extra})
            return

        magnet = victim.get("magnet_uri") or a.args.get("magnet") or ""
        paths = self._claimed_paths(victim)          # 摘之前记下：摘了就问不到了
        self.ctx.qbit.delete([h], delete_files=False)
        self._removed_torrents.add(h)
        self._removed_subjects[h.lower()] = {**v.subject, "torrent_files": len(paths)}

        self._audit("applied", f, a,
                    {"dropped": victim.get("name", ""),
                     "dropped_progress": round(victim.get("progress", 0), 3),
                     "kept": (keeper or {}).get("name", ""),
                     "files_untouched": True,
                     **({} if magnet else {"note": "无 magnet_uri，此条不可回退"}),
                     **extra},
                    undo=self._readd_undo(victim, magnet, paths))

    def _claimed_paths(self, victim: dict) -> list[str]:
        """这个种子此刻声明着的绝对路径（优先级非 0 的条目），写进 `readd_torrent` 逆操作，
        回退重加前拿来问占用。读不到返回 []——这只是回退的线索，不该拦住摘除本身
        （回退时退回到记录里的 `args.path` 与显示名）。"""
        sp = (victim.get("save_path") or "").rstrip("/")
        if not sp:
            return []
        try:
            entries = self._claims().entries(victim.get("hash", ""))
        except ClaimsUnknown:
            return []
        return [str(Path(sp) / e["name"]) for e in entries if e.get("priority", 1) != 0]

    @staticmethod
    def _readd_undo(victim: dict, magnet: str, paths: list[str] | None = None) -> dict | None:
        """摘掉一条种子记录的逆操作：凭 magnet 按原布局加回来。

        magnet 是唯一的回退凭据：删掉之后 .torrent 就没了，只能靠 magnet
        把这条记录重新加回来。取不到就返回 None，调用方要明说不可回退。

        `paths`：摘的时候它声明着的路径。回退重加前逐个问占用——此后被别的种子
        占了，加回来就是两个种子争一个文件（critic N6）。
        """
        if not magnet:
            return None
        undo = {"op": "readd_torrent", "magnet": magnet,
                "save_path": victim.get("save_path", ""),
                "category": victim.get("category", ""),
                "tags": victim.get("tags", ""),
                "name": victim.get("name", ""),
                # 没有根目录（`root_path` 为空）= NoSubfolder 或单文件。
                # 按原布局加回来，已下的半成品才对得上；Original 重加会
                # 多长一层根目录，半成品全部失联。
                "no_subfolder": not victim.get("root_path")}
        if paths is not None:
            undo["paths"] = list(paths)
        return undo

    def _op_relink_torrent(self, f: Finding, a: Action) -> None:
        """把路径失效的种子重新关联到磁盘上的实际文件。

        `renameFile` 在这里不是"改文件名"而是"改种子对文件的期望路径"——
        目标文件已经存在，qBittorrent 只更新自己的映射。随后 `recheck`
        让 libtorrent 逐分片校验哈希：**校验通过才算真的修好了**，
        这一步是这套修复能自动做的依据（大小匹配只是定位手段，哈希才是证据）。
        """
        h = a.args["torrent_hash"]
        mapping = a.args["mapping"]
        new_save_path = a.args.get("new_save_path") or ""

        # 先问占用，再做任何改动（critic N6）。检测器按"字节数唯一"定位，从不看那个
        # 文件是不是已经归另一个活种子；关联过去就是两个种子声明同一个文件，大小相同
        # 而内容不同时 recheck 把分片判缺，qBittorrent 会重下、覆盖到别人的文件上。
        try:
            cur = self._claims().torrent(h)
        except ClaimsUnknown as e:
            self._audit("failed", f, a, {
                "error": f"无法确认目标路径的占用情况，未做任何改动：{e}"})
            return
        if cur is None:
            self._audit("skipped", f, a, {"reason": "种子已不在 qBittorrent 里"})
            return
        cur_sp = (cur.get("save_path") or "").rstrip("/")
        conflicts = self._relink_conflicts(h, cur_sp, mapping, new_save_path,
                                           disk_for_mapped=False)
        unknown = next((c for c in conflicts if c.unknown), None)
        if unknown:
            self._audit("failed", f, a, {
                "error": f"无法确认目标路径的占用情况，未做任何改动：{unknown.unknown}",
                "claims": [c.audit() for c in conflicts]})
            return
        if conflicts:
            self._audit("skipped", f, a, {
                "reason": ("目标文件已归另一个活种子（" + "；".join(c.describe(1) for c in conflicts[:2])
                           + "），关联过去就是两个种子争同一个文件，recheck 还会按自己的"
                             "分片去校验、重下它"),
                "claims": [c.audit() for c in conflicts]})
            return
        if self.dry_run:
            self._audit("skipped", f, a,
                        {"reason": "dry-run", "would_relink": len(mapping),
                         "would_relocate_to": new_save_path})
            return

        prev_save_path = cur_sp if new_save_path else ""
        if new_save_path:
            # 文件已搬到别的目录：先把 save_path 挪过去，renameFile 用的是
            # 相对 save_path 的路径，跨不出去。
            self.ctx.qbit.set_location([h], new_save_path)

        before = {e["name"] for e in self.ctx.qbit.files(h)}
        renamed, failed = [], []
        for m in mapping:
            if m["old"] not in before:
                failed.append({**m, "error": "种子文件列表里已无此条目"})
                continue
            try:
                self.ctx.qbit.rename_file(h, m["old"], m["new"])
                renamed.append(m)
            except Exception as e:
                failed.append({**m, "error": str(e)})

        if not renamed:
            self._audit("failed", f, a, {"error": "没有任何文件重建关联成功",
                                         "failures": failed[:3]})
            return

        # 交给 libtorrent 校验哈希；结果异步产生，这里只负责触发
        self.ctx.qbit.recheck([h])
        self._audit("applied", f, a,
                    {"relinked": len(renamed), "failed": len(failed),
                     "relocated_to": new_save_path, "recheck_triggered": True,
                     "note": "recheck 为异步，稍后确认 progress 回到 100%"},
                    undo={"op": "relink_torrent",
                          "torrent_hash": h,
                          "new_save_path": prev_save_path,
                          "mapping": [{"old": m["new"], "new": m["old"]} for m in renamed]})

    def _relink_conflicts(self, h: str, cur_sp: str, mapping: list[dict], new_sp: str,
                          *, disk_for_mapped: bool) -> list[ClaimCheck]:
        """relink（或它的逆操作）之后这个种子的每个条目会落在哪、那里此刻有没有别人。

        - 映射里的条目落到 `(new_sp or cur_sp)/映射后的名字`。正向时那个文件本来就该在盘上
          （就是要关联过去的那个），只问 qBittorrent（`disk_for_mapped=False`）；逆向时
          目标是原来失联的路径，盘上此后若有了别的文件同样算占用。
        - 换目录时（`new_sp` 非空）不在映射里的条目也会被 setLocation 连带搬过去，
          它们的目的地一并查（盘上 + qBittorrent）。
        返回不空闲的查询结果（被占或看不全）。
        """
        claims = self._claims()
        moved = {m.get("old"): m.get("new") for m in mapping}
        dest_root = Path(new_sp or cur_sp or "/")
        out: list[ClaimCheck] = []
        try:
            entries = claims.entries(h)
        except ClaimsUnknown as e:
            return [ClaimCheck(str(dest_root), unknown=str(e))]
        for e in entries:
            if e.get("priority", 1) == 0:
                continue
            name = e["name"]
            own = Path(cur_sp or "/") / name
            if name in moved:
                chk = claims.check(dest_root / moved[name], own_hash=h, own_path=own,
                                   disk=disk_for_mapped)
            elif new_sp:
                chk = claims.check(dest_root / name, own_hash=h, own_path=own)
            else:
                continue
            if not chk.free:
                out.append(chk)
        return out

    def _op_delete_category(self, f: Finding, a: Action) -> None:
        """删除空分类。只动分类定义，不碰任何文件。

        执行前重新核对该分类确实已无种子——合并动作可能在本轮早些时候失败，
        那样这个分类里还留着东西，删了会让它们变成"无分类"。
        """
        cat = a.args["category"]
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        still = [t for t in self.ctx.qbit.torrents() if (t.get("category") or "") == cat]
        if still:
            self._audit("skipped", f, a,
                        {"reason": f"该分类仍有 {len(still)} 个种子，可能是合并失败，不删"})
            return
        self.ctx.qbit.remove_categories([cat])
        self._audit("applied", f, a)

    def _op_relocate(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        self.ctx.qbit.set_location([a.args["torrent_hash"]], a.args["location"])
        self._audit("applied", f, a)

    def _op_write_nfo(self, f: Finding, a: Action) -> None:
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return
        nfo = Path(a.args["path"])
        nfo.parent.mkdir(parents=True, exist_ok=True)
        nfo.write_text(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<tvshow>\n"
            f"  <title>{a.args['title']}</title>\n"
            f"  <originaltitle>{a.args.get('original_title','')}</originaltitle>\n"
            f'  <uniqueid type="tmdb" default="true">{a.args["tmdb_id"]}</uniqueid>\n'
            f"  <tmdbid>{a.args['tmdb_id']}</tmdbid>\n"
            "</tvshow>\n",
            encoding="utf-8",
        )
        self._audit("applied", f, a)

    def _op_rename_show_dir(self, f: Finding, a: Action) -> None:
        """改番剧目录名，并同步 AutoBangumi 的 save_path。

        不同步 save_path 的话，下次新集数会重新建出旧目录，整理工作周期性重演。
        """
        old = Path(a.args["path"])
        new = old.parent / a.args["new_name"]

        # 改动前记下所有受影响种子的原始 save_path，供回退使用
        # 用 under() 而不是 startswith：见 kernel.under 的注释——
        # 这一行曾把兄弟目录的种子圈进来，把库里三个目录名叠成了一团。
        try:
            listed = self._claims().torrents()
        except ClaimsUnknown as e:
            self._audit("failed", f, a, {
                "error": f"无法确认目标目录的占用情况，未做任何改动：{e}"})
            return
        affected = [(t["hash"], t.get("save_path") or "")
                    for t in listed.values()
                    if under(t.get("content_path") or "/\0", old)
                    or under(t.get("save_path") or "/\0", old)]

        # 目的地此刻归谁（critic N6 的目录形态）。以前只看 `new.exists()`：盘上还没有，
        # 不等于没人占——按新标题建的订阅、手动加的种子，save_path 可能已经指到它下面
        # （0%、没元数据的也算），或者一个 save_path 在媒体根、根文件夹就叫这个名字的
        # Original 布局种子。搬进去两边的文件混在一个目录里，同名集位互相争。
        # 要搬的那些（affected）不算；只差大小写的同名目录按"已存在"算（claims.check_dir）。
        chk = self._claims().check_dir(new, movers=[h for h, _ in affected])
        if chk.unknown:
            self._audit("failed", f, a, {
                "error": f"无法确认目标目录的占用情况，未做任何改动：{chk.unknown}",
                "claims": chk.audit()})
            return
        if chk.on_disk:
            self._audit("skipped", f, a, {"reason": "目标目录已存在，需人工合并",
                                          "claims": chk.audit()})
            return
        if chk.in_qbit:
            self._audit("skipped", f, a, {
                "reason": ("目标目录下已有别的种子声明的路径（" + chk.describe()
                           + "），搬进去会与它们混在一起，需人工合并"),
                "claims": chk.audit()})
            return
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run"})
            return

        bid = a.args.get("bangumi_id")
        prev_savepath = ""
        if bid and self.ctx.abdb:
            rows = self.ctx.abdb.query("SELECT save_path FROM bangumi WHERE id=?", (bid,))
            if rows:
                prev_savepath = rows[0]["save_path"] or ""

        # === 由 qBittorrent 搬运，而不是自己 mv 目录 ===
        # 裸 mv 会让所有种子的记录路径瞬间失效（实测已因此产生 28 个死链种子）。
        # setLocation 让 qBittorrent 自己移动文件并同步记录，做种不中断。
        moved_ok, move_failed = [], []
        for h, sp in affected:
            try:
                self.ctx.qbit.set_location([h], repath(sp, old, new))
                moved_ok.append(h)
            except Exception as e:
                move_failed.append({"hash": h, "error": str(e)})

        if move_failed:
            # 有种子没搬成功就中止：此时目录处于半迁移状态，
            # 继续 mv 剩余文件只会让情况更糟，交给人处理。
            self._audit("failed", f, a,
                        {"error": f"{len(move_failed)} 个种子 setLocation 失败，已中止目录改名",
                         "moved_ok": len(moved_ok), "failures": move_failed[:3]},
                        undo={"op": "rename_show_dir_partial",
                              "moved_hashes": moved_ok,
                              "torrent_savepaths": affected})
            return

        # 种子搬完后，把没有种子关联的残留文件（NFO、孤儿字幕、失联种子的文件等）挪过去
        leftovers, stranded = self._merge_tree(old, new)

        if bid and self.ctx.abdb and prev_savepath:
            self.ctx.abdb.write([
                ("UPDATE bangumi SET save_path=? WHERE id=?",
                 (repath(prev_savepath, old, new)
                  if under(prev_savepath, old) else prev_savepath, bid))
            ])

        self._audit("applied", f, a,
                    {"new_path": str(new), "torrents_moved": len(moved_ok),
                     "leftover_files_moved": leftovers,
                     "stranded_files": stranded,
                     "old_dir_removed": not old.exists()},
                    undo={"op": "rename_show_dir", "path": str(new),
                          "new_name": old.name, "bangumi_id": bid,
                          "prev_savepath": prev_savepath,
                          "torrent_savepaths": affected})

    def _op_trash(self, f: Finding, a: Action) -> None:
        """删除 = 移入隔离区。受配额上限保护，**动手前过删除关口**（`media_agent/gate.py`）。

        **顺序就是这个动作的安全性所在**，每一步都只在前一步确定成功后才走：

        1. 演进规则产出的删除直接拒绝（`gate.screen`，critic N5）。
        2. 要搬的必须是媒体库里一个**真实存在的普通文件**——在碰 qBittorrent 之前核对。
           以前先 `qbit.delete` 后看 `path.exists()`：路径是幻影（种子声明了、
           盘上没有）或诊断后被挪走时，种子记录丢了、文件一个没搬，还写下一条
           `trash_path: ""` 的逆操作（critic N1）。生产实例：20260920T170126
           删掉朱音落语 S01E12 所属种子 d08f05a7 的记录，`freed 0`。
           目录一律拒绝：死种的 content_path 对 NoSubfolder 多文件种子就是整个
           Season 目录（生产 10 个），`st_size` 还只有目录项那点大，体积配额拦不住。
        3. **删除关口**：按此刻的 qBittorrent 与磁盘复核 I1–I4（见 gate 模块文档），
           并决定怎么处置种子——多文件种子只作废这一个条目（I3），条目按完整路径认。
           拒绝记 skipped（「删除关口：Ix …」），看不全记 failed。
        4. 配额、dry-run。
        5. 处理种子：整种子摘记录，或只把这一个文件设为不下载。**失败就停手**——
           以前只记一行日志照样搬文件，结果是种子还在、文件没了（qBittorrent
           会把它重新下回来），或种子记录的状态与审计对不上。
        6. 最后搬文件。搬失败时如实记下种子记录是否已经删掉。

        每条记录（applied / skipped / failed）都带 `deletion`：关口结论与 purge 要的事实。
        """
        from . import gate
        refused = gate.screen(f)
        if refused:
            self._audit("skipped", f, a, {
                "reason": refused,
                "deletion": {"gate": "evolved", "disposition": gate.disposition_of(f),
                             "rule": f.rule}})
            return
        path, why = _inside(a.args.get("path"), Path(self.cfg.media_root), "path")
        if why:
            self._audit("failed", f, a,
                        {"error": f"拒绝移入隔离区：{why}（只处置媒体库里的文件）"})
            return
        if not os.path.lexists(path):
            self._trash_absent(f, a, path)
            return
        if path.is_dir():
            self._audit("failed", f, a, {
                "error": f"拒绝整目录移入隔离区：{path} 是目录——隔离只处置单个文件"})
            return
        if not path.is_file():
            self._audit("failed", f, a, {"error": f"拒绝移入隔离区：{path} 不是普通文件"})
            return
        if a.args.get("torrent_hash") and not self.ctx.qbit:
            # 纵深防御（apply 的总闸之外）：种子不处理就搬文件，qBittorrent 会
            # 继续宣称这个路径，下一轮 scan 把它当成幻影、或重新下回来。
            self._audit("skipped", f, a, {
                "reason": "有种子的文件，但 qBittorrent 不可用：拒绝只搬文件、不处理种子"})
            return

        v = gate.check_trash(self, f, path)
        extra = v.audit()
        if v.failed:
            self._audit("failed", f, a, {"error": v.failed, **extra})
            return
        if v.refused:
            self._audit("skipped", f, a, {"reason": v.refused, **extra})
            return
        size = path.stat().st_size

        # 配额检查
        if self._deleted_count >= self.cfg.max_delete_per_run:
            self._audit("skipped", f, a, {
                "reason": f"已达单轮删除数量上限 {self.cfg.max_delete_per_run}", **extra})
            return
        if (self._deleted_bytes + size) / 1e9 > self.cfg.max_delete_gb_per_run:
            self._audit("skipped", f, a, {
                "reason": f"已达单轮删除体积上限 {self.cfg.max_delete_gb_per_run}GB", **extra})
            return

        # 先看隔离区放不放得下（critic N8）：隔离区与媒体是同一个 APFS 容器里的两个卷，搬运是
        # 先拷后删、要多占一整份。以前放不下要等拷到一半 ENOSPC 才知道——那时种子那一步已经做了
        # （整种子摘掉或设为不下载），文件却还在原处，隔离区里还多半个拷贝。放不下就跳过、都不动。
        room = disposal.room_problem(self.cfg.trash_dir, size, "隔离区")
        if room:
            self._audit("skipped", f, a, {"reason": room, **extra})
            return

        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run", "would_free_bytes": size,
                                          **extra})
            return

        # 种子怎么处置由关口定（I3）：多文件种子只作废这一个条目，只剩它一个时整种子作废
        # （把唯一的文件设为不下载 = 留下一个指向空内容的孤儿种子，被 stale-torrent-path
        # 报成"路径失效且无法自动定位"——攻壳机动队实测留下 6 个）。本批次已摘掉的种子、
        # 诊断后被别人删掉的种子，关口给的 `torrent_hash` 为空：只搬文件。
        h = v.torrent_hash
        record_lost = zeroed = False
        if h and not v.file_only:
            # 整个种子作废：先删种子记录（不删文件），文件再单独进隔离区
            try:
                self.ctx.qbit.delete([h], delete_files=False)
            except Exception as e:
                self._audit("failed", f, a, {
                    "error": f"删除种子记录失败，文件未动：{type(e).__name__}: {e}", **extra})
                return
            record_lost = True
            self._removed_torrents.add(h)
            self._removed_subjects[h] = v.subject
        elif h:
            # 只作废种子里的某个文件：设为不下载，保留其余部分
            try:
                self.ctx.qbit.set_file_priority(h, [v.entry["index"]], 0)
            except Exception as e:
                self._audit("failed", f, a, {
                    "error": f"设为不下载失败，文件未动：{type(e).__name__}: {e}", **extra})
                return
            zeroed = True

        day = datetime.now().strftime("%Y-%m-%d")
        dest_dir = self.cfg.trash_dir / day / f.show / (path.parent.name or "")
        dest = dest_dir / path.name
        if os.path.lexists(dest):
            dest = dest_dir / f"{path.stem}.{int(time.time())}{path.suffix}"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
        except Exception as e:
            # 种子那一步已经做了、文件没搬走：如实记下，别只剩一句异常
            # （跨卷搬运是先拷后删，磁盘满时就会在这里失败，critic N8）。
            self._audit("failed", f, a, {
                "error": f"搬入隔离区失败：{type(e).__name__}: {e}",
                "torrent_record_lost": record_lost, "priority_zeroed": zeroed,
                "path_still_at": str(path), **extra})
            return
        self._deleted_count += 1
        self._deleted_bytes += size
        self._trashed_paths.add(str(path))

        # 文件可从隔离区还原；但被删掉的种子记录还原不了（种子文件本身已不在）
        undo = {"op": "restore_from_trash", "path": str(path),
                "trash_path": str(dest), "torrent_record_lost": record_lost}
        self._audit("applied", f, a, {"trashed_to": str(dest), "freed_bytes": size, **extra},
                    undo=undo)

    _GONE = ("文件已不在原位（种子声明了但盘上没有，或诊断后被挪走），种子与文件都不动")

    def _trash_absent(self, f: Finding, a: Action, path: Path) -> None:
        """要隔离的文件不在盘上：没有文件可搬，能处置的只剩种子那一侧。

        **幻影**（诊断时就是"种子说已下完、盘上没有"，由检测器标 `phantom`，
        此刻复核仍然如此）：摘掉这条种子记录，或合集里只把这个条目设为不下载。
        只跳过是不够的——第 2 条修复之后幻影输家"种子与文件都不动"，同一批里
        `_op_rename` 就把赢家改到幻影仍在声明的名字上，两个种子宣称同一路径，
        此后每轮都无声无息（2026-09-26 审查复现；main 在同一轮会摘掉它的记录）。
        逆操作是 magnet 重加 / 恢复优先级，**绝不**写 `restore_from_trash`——
        隔离区里没有东西，那正是 LAT-02 空 `trash_path` 的来源。

        **还没下完的 `file_only` 条目**（extras-in-library 对合集里的 NCOP / PV，
        判重对合并发布的另一版本）：盘上只有 `.!qB` 或什么都没有，本来就不该"在"。
        立刻设为不下载——main 一直是这么做的；第 2 条修复之后它被当成"文件不在"
        跳过，特典照下不误（占带宽、占 94% 满的容器，critic N8），下完下一轮才隔离，
        停滞的合集则每轮写一条误导的跳过记录。种子只剩这一个要下的文件时（单文件
        PV / CM 种子）摘掉记录，半成品留在原地（与 main 相同）。

        其余情形一律跳过、种子与文件都不动：诊断之后才被挪走的（种子多半该
        relink 而不是摘）、种子已不再声明这个路径的、又开始下载的幻影。
        """
        h = a.args.get("torrent_hash") or ""
        file_only = bool(a.args.get("file_only"))
        if (not h or not (a.args.get("phantom") or file_only)
                or h in self._removed_torrents):
            self._audit("skipped", f, a, {"reason": self._GONE})
            return
        if not self.ctx.qbit:
            self._audit("skipped", f, a, {
                "reason": "有种子的文件不在盘上，而 qBittorrent 不可用：拒绝处置种子"})
            return
        victim = next((t for t in self.ctx.qbit.torrents() if t["hash"] == h), None)
        if victim is None:
            self._audit("skipped", f, a, {"reason": "所属种子已不在 qBittorrent 里"})
            return
        try:
            entries = self.ctx.qbit.files(h)
        except Exception as e:
            self._audit("failed", f, a, {
                "error": f"读取种子文件列表失败，未做任何改动：{type(e).__name__}: {e}"})
            return
        sp = Path(victim.get("save_path") or "")
        entry = next((e for e in entries if sp / e["name"] == path), None)
        if entry is None or entry.get("priority", 1) == 0:
            self._audit("skipped", f, a, {
                "reason": "种子已不再声明这个路径（改过名或已设为不下载），不用再处置"})
            return
        wanted = [e for e in entries if e.get("priority", 1) != 0]
        from . import gate
        v = gate.Verdict(gate.disposition_of(f), torrent_hash=h, entry=entry)
        gate.describe(v, f, path, h, victim, wanted, Path(self.cfg.media_root))
        if file_only and entry.get("progress", 0) < 1:
            if len(wanted) > 1:
                self._zero_priority(f, a, h, entry,
                                    "还没下完：设为不下载，不必等它下完再隔离", v.audit())
            else:
                self._drop_record(f, a, victim,
                                  "种子只剩这一个要下的文件且还没下完：摘掉记录，"
                                  "半成品留在原地", v.audit())
            return
        if not a.args.get("phantom"):
            self._audit("skipped", f, a, {"reason": self._GONE})
            return
        if entry.get("progress", 0) < 1 or os.path.lexists(str(path) + ".!qB"):
            self._audit("skipped", f, a, {"reason": "种子又在下载这个文件，不是幻影了"})
            return
        # 删除关口 I3 对幻影同样成立：合集里的一个幻影条目只作废它自己，不摘整个合集
        # （以前没标 file_only 的幻影输家——判重的普通输家——会把整个合集的记录摘掉）。
        if len(wanted) > 1:
            if not file_only:
                v.notes.append(gate.i3_note(wanted, entry))
            self._zero_priority(f, a, h, entry,
                                "幻影：盘上没有这个文件，只把这个条目设为不下载，种子其余部分照常做种",
                                v.audit())
        else:
            self._drop_record(f, a, victim,
                              "幻影：盘上没有可搬的文件，只摘种子记录（可凭 magnet 回退）",
                              v.audit())

    def _zero_priority(self, f: Finding, a: Action, h: str, entry: dict, note: str,
                       extra: dict | None = None) -> None:
        """只把种子里的一个条目设为不下载，盘上一个字节都不动。"""
        extra = extra or {}
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run", "would_zero": entry["name"],
                                          **extra})
            return
        try:
            self.ctx.qbit.set_file_priority(h, [entry["index"]], 0)
        except Exception as e:
            self._audit("failed", f, a, {
                "error": f"设为不下载失败，未做任何改动：{type(e).__name__}: {e}", **extra})
            return
        self._audit("applied", f, a,
                    {"priority_zeroed": True, "at": entry["name"], "files_untouched": True,
                     "note": note, **extra},
                    undo={"op": "restore_file_priority", "torrent_hash": h,
                          "index": entry["index"], "name": entry["name"],
                          "priority": entry.get("priority", 1)})

    def _drop_record(self, f: Finding, a: Action, victim: dict, note: str,
                     extra: dict | None = None) -> None:
        """只摘种子记录（`delete_files=False`），盘上一个字节都不动。"""
        extra = extra or {}
        h = victim["hash"]
        if self.dry_run:
            self._audit("skipped", f, a, {"reason": "dry-run",
                                          "would_drop": victim.get("name", ""), **extra})
            return
        magnet = victim.get("magnet_uri") or ""
        paths = self._claimed_paths(victim)          # 摘之前记下：摘了就问不到了
        try:
            self.ctx.qbit.delete([h], delete_files=False)
        except Exception as e:
            self._audit("failed", f, a, {
                "error": f"删除种子记录失败，未做任何改动：{type(e).__name__}: {e}", **extra})
            return
        self._removed_torrents.add(h)
        if (extra.get("deletion") or {}).get("subject"):
            self._removed_subjects[h.lower()] = extra["deletion"]["subject"]
        self._audit("applied", f, a,
                    {"dropped": victim.get("name", ""), "files_untouched": True, "note": note,
                     **({} if magnet else {"irreversible": "无 magnet_uri，此条不可回退"}),
                     **extra},
                    undo=self._readd_undo(victim, magnet, paths))

    # ---------------- 回退 ----------------
    def rollback(self, run_id: str) -> dict:
        """把某一次 run 的所有改动逆序还原。

        逆序（LIFO）是必须的：先改文件名再改目录名的话，回退必须先还原目录名，
        否则文件的路径已经不对了。

        每一步都先核对当前状态与预期一致才动手——如果之后又有别的改动叠加上来，
        宁可跳过并报告，也不要盲目覆盖。
        """
        records = self._read_audit(run_id)
        undoable = [r for r in records if r.get("status") == "applied" and r.get("undo")]
        no_undo = [r for r in records
                   if r.get("status") == "applied" and not r.get("undo")]

        done, skipped, failed, lost = [], [], [], []

        # qBittorrent 不在就整批拒绝（critic N3）：逆改名会退化成 `mv`，
        # 目录改名的逆操作无从得知哪些文件归活种子，其余逆操作
        # 各自 AttributeError 记成 failed——半截回退比不回退更难收拾。
        # （qBit 在线时，目录改名的逆操作另有一道：先问活的种子视图，有不在记录里
        # 的种子就不动；setLocation 失败记 failed、残留一个不搬。）
        # 也**不写** rollback 汇总记录：写了 `list_runs` 就会把这批标成已回退。
        refused = self.qbit_blocker()
        if refused:
            return {"run_id": run_id, "refused": refused, "total": len(records),
                    "reverted": 0, "skipped": 0, "failed": 0,
                    "irreversible": len(no_undo), "torrent_records_lost": 0,
                    "skipped_detail": [], "failed_detail": []}

        for rec in reversed(undoable):        # LIFO
            u = rec["undo"]
            try:
                ok, reason = self._apply_undo(u, rec)
                if ok:
                    done.append(rec)
                else:
                    skipped.append({**rec, "skip_reason": reason})
            except Exception as e:
                failed.append({**rec, "error": f"{type(e).__name__}: {e}"})
            if self._claim_index is not None:
                # 每一步逆操作之后占用索引作废（失败的也可能改到一半），下一步看到的是新状态
                self._claim_index.invalidate()
            if u.get("torrent_record_lost"):
                lost.append(rec)

        result = {
            "run_id": run_id,
            "total": len(records),
            "reverted": len(done),
            "skipped": len(skipped),
            "failed": len(failed),
            "irreversible": len(no_undo),
            "torrent_records_lost": len(lost),
            "skipped_detail": skipped[:10],
            "failed_detail": failed[:10],
            "refused": "",
        }
        if not self.dry_run:
            with self.cfg.audit_log.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "run_id": f"rollback-of-{run_id}",
                    "status": "rollback",
                    **result,
                }, ensure_ascii=False) + "\n")
        return result

    def _read_audit(self, run_id: str) -> list[dict]:
        if not self.cfg.audit_log.exists():
            return []
        out = []
        for line in self.cfg.audit_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id and not rec.get("dry_run"):
                out.append(rec)
        return out

    def _undo_problem(self, u: dict) -> str | None:
        """逆操作的参数是否合法；合法返回 None，否则返回拒绝原因。

        审计记录是**历史数据**：写它的代码可能有 bug（6 条 `trash_path` 为空
        的记录就是 `_op_trash` 先删种子、后查文件留下的），也可能被手工改过。
        回退又是最危险的时刻——它照着记录去搬文件、改种子。所以每个逆操作
        都先核对自己的输入，形状不对就拒绝并说明原因，绝不"尽力而为"。
        """
        op = u.get("op")
        media = Path(self.cfg.media_root)

        def lib_path(key: str) -> str | None:
            return _inside(u.get(key), media, key)[1]

        if op == "restore_from_trash":
            return (_inside(u.get("trash_path"), self.cfg.trash_dir, "trash_path")[1]
                    or lib_path("path"))
        if op == "rename":
            return lib_path("path") or _bad_name(u.get("new_name"), "new_name")
        if op == "rename_show_dir":
            why = lib_path("path") or _bad_name(u.get("new_name"), "new_name")
            if why:
                return why
            for pair in u.get("torrent_savepaths") or []:
                if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0]):
                    return f"torrent_savepaths 条目不合法：{pair!r}"
                why = _inside(pair[1], media, "torrent_savepaths 的 save_path")[1]
                if why:
                    return why
            prev = u.get("prev_savepath")
            if prev and not os.path.isabs(str(prev)):
                return f"prev_savepath 不是绝对路径：{prev!r}"
            return None
        if op in ("restore_sidecar", "ungrab_episode"):
            why = lib_path("show_dir")
            if why:
                return why
            if op == "ungrab_episode":
                try:
                    int(u["season"]), int(u["episode"])
                except (KeyError, TypeError, ValueError):
                    return "season / episode 缺失或不是整数"
            return None
        if op in ("restore_title_aliases", "restore_rss_link"):
            if not u.get("bangumi_id"):
                return "缺 bangumi_id"
            if not self.ctx.abdb:
                return "AutoBangumi 数据库不可用"
            return None
        if op == "readd_torrent":
            magnet = u.get("magnet") or ""
            if not (isinstance(magnet, str) and magnet.startswith("magnet:?")
                    and "xt=urn:btih:" in magnet):
                return f"magnet 不合法：{magnet!r}"
            paths = u.get("paths")
            if paths is not None:
                if not isinstance(paths, list):
                    return f"paths 不是列表：{paths!r}"
                for p in paths:
                    why = _inside(p, media, "paths 的条目")[1]
                    if why:
                        return why
            if u.get("save_path"):
                return lib_path("save_path")
            return None
        if op == "relink_torrent":
            if not u.get("torrent_hash"):
                return "缺 torrent_hash"
            for m in u.get("mapping") or []:
                why = (_bad_rel((m or {}).get("old"), "mapping.old")
                       or _bad_rel((m or {}).get("new"), "mapping.new"))
                if why:
                    return why
            if u.get("new_save_path"):
                return lib_path("new_save_path")
            return None
        if op in ("recategorize", "remove_tags"):
            if not u.get("torrent_hash"):
                return "缺 torrent_hash"
            if op == "remove_tags" and not u.get("tags"):
                return "缺 tags"
            return None
        if op == "restore_file_priority":
            if not u.get("torrent_hash"):
                return "缺 torrent_hash"
            idx, pri = u.get("index"), u.get("priority")
            if not (isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0):
                return f"index 不是非负整数：{idx!r}"
            if not (isinstance(pri, int) and not isinstance(pri, bool) and 1 <= pri <= 7):
                return f"priority 不是 1–7 的整数：{pri!r}"
            return None
        return None

    def _readd_paths(self, u: dict, args: dict) -> list[Path]:
        """重加之后这个种子可能声明的路径：摘除时记下的 `paths`、正向动作的 `args.path`
        （撞车 / 幻影的那个路径），以及 `save_path/显示名`——单文件种子的显示名就是它原本
        的文件名，磁力重加回来的正是这个名字（第 2 阶段之前的记录只有它可用）。
        只留媒体库之下的规范绝对路径。"""
        media = Path(self.cfg.media_root)
        raw = list(u.get("paths") or [])
        if args.get("path"):
            raw.append(args["path"])
        sp, name = (u.get("save_path") or "").rstrip("/"), u.get("name") or ""
        if sp and not _bad_name(name, "name"):
            raw.append(str(Path(sp) / name))
        out: list[Path] = []
        for p in raw:
            q = _inside(p, media, "path")[0]
            if q is not None and q not in out:
                out.append(q)
        return out

    def _apply_undo(self, u: dict, rec: dict | None = None) -> tuple[bool, str]:
        """执行一条逆操作。返回 (是否成功, 跳过原因)。`rec` 是它所在的整条审计记录。"""
        op = u.get("op")
        bad = self._undo_problem(u)
        if bad:
            return False, f"逆操作参数不合法，拒绝执行（{op}）：{bad}"

        if op == "rename":
            cur = Path(u["path"])
            if not cur.exists():
                return False, f"当前文件不存在，可能已被再次改名：{cur.name}"
            if cur.is_dir():
                return False, f"逆改名的对象是目录而不是文件，拒绝：{cur}"
            back = cur.parent / u["new_name"]
            # AGENTS.md 第 3 条对回退同样成立：有种子的文件只走 renameFile。
            # 以前种子里找不到、或 qBit 不在时，这里退化成 `Path.rename`（critic N3）。
            h = u.get("torrent_hash")
            if h and not self.ctx.qbit:
                return False, "有种子的文件，但 qBittorrent 不可用：拒绝绕过它改名"
            # 原名此刻归谁（critic N6）。以前只看 `back.exists()`：原名此后被一个还没落盘的
            # 下载映射了就照改——两个种子声明同一路径；自己当初只改了大小写时，APFS 上
            # `exists()` 为真（就是它自己），回退永远跳过。
            chk = self._claims().check(back, own_hash=h or "", own_path=cur)
            if chk.unknown:
                return False, f"无法确认还原目标的占用情况，未做任何改动：{chk.unknown}"
            if chk.in_qbit:
                return False, f"还原目标仍被另一个种子声明：{chk.describe()}"
            if chk.claimants:
                return False, f"还原目标已存在：{back.name}（{chk.describe()}）"
            rel = None
            if h:
                rel = self._torrent_rel_path(h, cur)
                if rel is None:
                    return False, "种子文件列表里找不到该文件，拒绝退化成文件系统改名"
            if self.dry_run:
                return True, ""
            if rel is not None:
                new_rel = (str(Path(rel).parent / u["new_name"])
                           if "/" in rel else u["new_name"])
                self.ctx.qbit.rename_file(h, rel, new_rel)
            else:
                cur.rename(back)
            return True, ""

        if op == "rename_show_dir":
            cur = Path(u["path"])
            back = cur.parent / u["new_name"]

            # 回退同样由 qBittorrent 搬运（AGENTS.md 第 3 条）。以前这里有两个洞：
            # setLocation 的异常被 `continue` 吞掉，而改名之后才落进新目录的种子
            # （此后抓的新集）根本不在 torrent_savepaths 里——接下来按顶层把新目录
            # 整个 shutil.move 回去，它们的文件就被文件系统搬走、种子失联，
            # 这条记录还算 reverted。生产上有 78 条已执行的 rename_show_dir，
            # 回退其中较早的任何一条都会命中后一种，不需要任何故障。
            # 所以先问 qBittorrent 此刻谁在这个目录里：有不在记录里的，整条不动。
            if not cur.is_dir():
                # 改名后的目录不在了（此后又被改过名、或被人挪走）：记录里的种子此刻在哪
                # 不知道，照记录把它们逐个 setLocation 回去就是盲目覆盖。与逆改名同口径。
                return False, f"当前目录已不存在，可能此后又改过名：{cur.name}"
            listed = dict(u.get("torrent_savepaths") or [])
            owners, claimed = self._claims().claims_under(cur)
            strangers = [h for h in owners if h not in listed]
            if strangers:
                names = "、".join(f"{h[:8]}（{owners[h].get('name', '')[:40]}）"
                                 for h in strangers[:3])
                return False, (f"{cur.name} 里有 {len(strangers)} 个种子不在当初的改名记录里"
                               f"（改名之后才落进来的，如 {names}）：回退只会搬回记录里的"
                               f"种子，其余种子的文件会被连带搬走、就此失联。交给人处理")
            # 还原目标此刻归谁：以前只看 `back.exists()`。旧目录盘上没有，但此后可能已有
            # 种子（按旧标题的订阅）把 save_path 指回了它——搬回去就混在一起（critic N6）。
            chk = self._claims().check_dir(back, movers=owners)
            if chk.unknown:
                return False, f"无法确认还原目标目录的占用情况，未做任何改动：{chk.unknown}"
            if chk.on_disk:
                return False, f"还原目标目录已存在：{back.name}"
            if chk.in_qbit:
                return False, (f"还原目标目录下已有别的种子（{chk.describe()}），"
                               f"搬回去会与它们混在一起，交给人处理")
            if self.dry_run:
                return True, ""

            moved, failed = 0, []
            for h, sp in listed.items():
                if h not in owners:
                    continue          # 已不在这个目录里（被删了、或此后挪去了别处）：不归这次回退管
                try:
                    self.ctx.qbit.set_location([h], sp)   # 还原为原始 save_path
                    moved += 1
                except Exception as e:
                    failed.append(f"{h[:8]}：{type(e).__name__}: {e}")
            if failed:
                # 与正向操作同一口径：有种子没搬成就停手，残留一个都不动——此时是
                # 半迁移状态，再用文件系统搬只会把没搬成的那个种子的文件也搬走。
                raise RuntimeError(
                    f"{len(failed)} 个种子 setLocation 失败（已交给 qBittorrent 搬回 {moved} 个），"
                    f"残留文件一个没动，需人工核对：{failed[0]}")

            # 没有种子声明的残留（NFO、孤儿字幕、`.extras` 里的东西……）逐个文件搬回，
            # 任何种子声明的路径都不碰：setLocation 是异步的，qBit 可能还没搬完。
            if cur.exists():
                self._merge_tree(cur, back, skip=claimed)

            bid, prev = u.get("bangumi_id"), u.get("prev_savepath")
            if bid and prev and self.ctx.abdb:
                self.ctx.abdb.write([
                    ("UPDATE bangumi SET save_path=? WHERE id=?", (prev, bid))
                ])
            return True, ""

        if op == "restore_sidecar":
            if self.dry_run:
                return True, ""
            from . import sidecar as sc_mod
            p = sc_mod.path_for(Path(u["show_dir"]))
            if u.get("prev") is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(u["prev"], encoding="utf-8")
            return True, ""

        if op == "restore_title_aliases":
            if self.dry_run:
                return True, ""
            self.ctx.abdb.write([
                ("UPDATE bangumi SET title_aliases=? WHERE id=?",
                 (u.get("prev"), u["bangumi_id"]))
            ])
            return True, ""

        if op == "ungrab_episode":
            # 只把集号从 have 里摘掉，**不动种子和文件**：
            # 回退的语义是"当作没抓过、下轮可重抓"，而不是"删掉已下的内容"。
            # 真要删种子，走 trash 动作，那条有隔离区和配额保护。
            if self.dry_run:
                return True, ""
            from . import sidecar as sc_mod
            d = Path(u["show_dir"])
            sc = sc_mod.load(d)
            info = sc.seasons.get(str(u["season"]))
            if info:
                info["have"] = [x for x in (info.get("have") or [])
                                if x != int(u["episode"])]
                sc_mod.save(d, sc)
            return True, ""

        if op == "readd_torrent":
            # 用 magnet 把种子加回来。加回来是 paused 的：撞车的那个文件
            # 现在归另一个种子管，让它一上来就开跑等于重演当初的问题，
            # 由人看过再决定要不要启动。
            #
            # 加之前先问占用（critic N6）：它声明过的路径此后被**别的**种子占了（死种被摘后
            # 换源抓来的新种子、幻影被摘后改到集位名上的赢家），加回来就是两个种子争一个
            # 文件。撞车受害者当初保留的那一方（`keep_hash`）不算——它本来就在，回退要恢复
            # 的正是那个"暂停着、等人看"的原状。只问 qBittorrent：盘上的半成品多半就是它
            # 自己留下的。
            args = (rec or {}).get("args") or {}
            cands = self._readd_paths(u, args)
            if not cands:
                return False, ("无法确定重加后它会占用哪些路径（记录里没有 paths / path / "
                               "可用的显示名），拒绝盲目重加；magnet 在审计记录里")
            exempt = [args["keep_hash"]] if args.get("keep_hash") else []
            h = _btih(u["magnet"])
            for p in cands:
                chk = self._claims().check(p, own_hash=h, disk=False, exempt=exempt)
                if chk.unknown:
                    return False, f"无法确认重加后的占用情况，未做任何改动：{chk.unknown}"
                if chk.claimants:
                    return False, (f"重加会与活种子争同一路径（{chk.describe()}）；magnet 在审计"
                                   f"记录里，确认没有冲突后可手动加回")
            if self.dry_run:
                return True, ""
            try:
                # 回滚重加要保持暂停：让人先确认再放行，别一回退就开跑。
                # 已存在（409）等同于回退成功。
                # 旧记录没有 no_subfolder 字段：保持原来的 False（撞车受害者都是
                # 单文件种子，布局对它们无影响）。
                self.ctx.qbit.add_torrent(
                    u["magnet"], paused=True,
                    no_subfolder=bool(u.get("no_subfolder", False)),
                    save_path=u.get("save_path") or "",
                    category=u.get("category") or "", tags=u.get("tags") or "")
            except Exception as e:
                return False, f"重新加种失败: {e}"
            return True, ""

        if op == "restore_rss_link":
            if self.dry_run:
                return True, ""
            # rss_link 和 aliases 是一次写进去的，回退也要一起还原，
            # 否则会留下"新链接配旧别名"或反之的半吊子状态
            stmts = [("UPDATE bangumi SET rss_link=?, title_aliases=? WHERE id=?",
                      (u.get("prev_rss_link") or "", u.get("prev_aliases"),
                       u["bangumi_id"]))]
            stmts += [("UPDATE rssitem SET url=? WHERE id=?",
                       (u.get("prev_rss_link") or "", i))
                      for i in u.get("rssitem_ids", [])]
            self.ctx.abdb.write(stmts)
            return True, ""

        if op == "relink_torrent":
            h = u["torrent_hash"]
            # 映射回原来（失联时）的路径之前先问占用（critic N6）：那个名字此后若被别的
            # 种子声明、或盘上有了别的文件，回退就造出两个种子争一个路径。
            cur = self._claims().torrent(h)
            if cur is None:
                return False, "种子已不在 qBittorrent 里"
            conflicts = self._relink_conflicts(
                h, (cur.get("save_path") or "").rstrip("/"), u.get("mapping") or [],
                u.get("new_save_path") or "", disk_for_mapped=True)
            unknown = next((c for c in conflicts if c.unknown), None)
            if unknown:
                return False, f"无法确认还原目标的占用情况，未做任何改动：{unknown.unknown}"
            if conflicts:
                return False, "还原目标已被占用：" + "；".join(c.describe(1) for c in conflicts[:2])
            if self.dry_run:
                return True, ""
            for m in u.get("mapping", []):
                try:
                    self.ctx.qbit.rename_file(h, m["old"], m["new"])
                except Exception:
                    continue
            if u.get("new_save_path"):        # 还原到原来的 save_path
                try:
                    self.ctx.qbit.set_location([h], u["new_save_path"])
                except Exception:
                    pass
            return True, ""

        if op == "recategorize":
            if self.dry_run:
                return True, ""
            self.ctx.qbit.set_category([u["torrent_hash"]], u.get("category") or "")
            return True, ""

        if op == "remove_tags":
            if self.dry_run:
                return True, ""
            self.ctx.qbit.remove_tags([u["torrent_hash"]], u["tags"])
            return True, ""

        if op == "restore_file_priority":
            h = u["torrent_hash"]
            try:
                entries = self.ctx.qbit.files(h)
            except Exception as e:
                if "404" in str(e):
                    return False, "所属种子已不在 qBittorrent 里"
                raise
            entry = next((e for e in entries if e.get("index") == u["index"]), None)
            if entry is None:
                return False, f"种子里已没有第 {u['index']} 个条目"
            if entry.get("priority", 1) != 0:
                return False, f"该条目的优先级已被改成 {entry.get('priority')}，不覆盖"
            # 优先级恢复成非 0，qBittorrent 就又要往这个路径写（critic N6）：此后若另一个
            # 种子映射到了同一个名字，恢复就是两个种子争一个文件。盘上不看——条目自己的
            # 文件（或半成品）本来就可能还在那里，分不出是谁的。
            t = self._claims().torrent(h)
            if t is None:
                return False, "所属种子已不在 qBittorrent 里"
            at = Path((t.get("save_path") or "").rstrip("/") or "/") / entry["name"]
            chk = self._claims().check(at, own_hash=h, own_path=at, disk=False)
            if chk.unknown:
                return False, f"无法确认这个条目路径的占用情况，未做任何改动：{chk.unknown}"
            if chk.claimants:
                return False, f"恢复下载会与活种子争同一路径：{chk.describe()}"
            if self.dry_run:
                return True, ""
            self.ctx.qbit.set_file_priority(h, [u["index"]], u["priority"])
            return True, ""

        if op == "restore_from_trash":
            # 路径形状已由 _undo_problem 核过（绝对、规范、分别在隔离区 / 媒体库之下）
            src = Path(os.path.normpath(u["trash_path"]))
            dst = Path(os.path.normpath(u["path"]))
            if not os.path.lexists(src):
                return False, "隔离区文件已不存在（可能已过保留期被清理）"
            if src.is_dir() or not src.is_file():
                # 隔离区里只该有单个文件。是目录就说明当初移进来的是整个目录
                # （死种按 content_path 删过整季目录的形态），整体搬回会和
                # 此后长出来的新内容搅在一起，交给人看。
                return False, f"隔离区里的不是普通文件，拒绝整体搬回：{src}"
            if os.path.lexists(dst):
                return False, f"原位置已被占用：{dst.name}"
            # 盘上空着不等于没人占（critic N6）：一个还没落盘的下载可能已映射到这个名字，
            # 搬回去之后它完成时 `X.!qB → X` 撞 EEXIST；只差大小写的文件在 APFS 上也是它。
            chk = self._claims().check(dst)
            if chk.unknown:
                return False, f"无法确认原位置的占用情况，未做任何改动：{chk.unknown}"
            if chk.claimants:
                return False, f"原位置已被占用：{chk.describe()}"
            # 搬回媒体库同样先拷后删（critic N8）：拷到一半 ENOSPC 会在库里留下一个截断的文件，
            # 下一轮扫描把它当成这一集
            room = disposal.room_problem(self.cfg.media_root, src.stat().st_size, "媒体库")
            if room:
                return False, room
            if self.dry_run:
                return True, ""
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return True, ""

        return False, f"未知逆操作 {op}"

    def repair_split_dirs(self, run_id: str) -> dict:
        """修复目录改名后新旧并存的分裂状态。

        场景：`rename_show_dir` 执行过但旧目录没清干净（早期版本的递归合并 bug，
        或 setLocation 失败导致半迁移）。这里按审计记录找出所有 old/new 配对，
        把旧目录残留内容合并进新目录。

        对**仍有有效种子**的文件优先走 qBittorrent setLocation；
        没有任何种子声明的残留才走文件系统。任何活种子此刻声明的路径都不用
        文件系统搬（`ClaimIndex.claims_under`）；某个种子 setLocation 失败，这一对就不合并。
        """
        # qBit 不在时"哪些文件有活种子"无从得知，_merge_tree 会用文件系统
        # 搬走活种子的文件（critic N3）。拒绝。
        refused = self.qbit_blocker()
        if refused:
            return {"pairs": 0, "detail": [], "refused": refused}
        pairs = []
        for rec in self._read_audit(run_id):
            if rec.get("status") != "applied" or rec.get("op") != "rename_show_dir":
                continue
            u = rec.get("undo") or {}
            # 与回退同一道闸（critic N1）：空 path 会让 new = Path('.')、
            # old = cwd 下的相对目录，_merge_tree 就在运维者的当前目录里搬文件。
            new, why = _inside(u.get("path"), Path(self.cfg.media_root), "path")
            if why or _bad_name(u.get("new_name"), "new_name"):
                continue
            old = new.parent / u["new_name"]
            if old.is_dir() and new.is_dir() and old != new:
                pairs.append((old, new))

        results = []
        for old, new in pairs:
            # 先问 qBittorrent 此刻谁在旧目录里有文件——不只是 save_path 在旧目录下的：
            # Original 布局、save_path 在媒体根、根目录恰好叫剧名的种子，content 在
            # 旧目录下而 save_path 不在，以前它不进 setLocation 名单，文件直接被
            # _merge_tree 用文件系统搬走。这些被声明的路径一律不许文件系统碰。
            owners, claimed = self._claims().claims_under(old)
            movable = []
            for t in owners.values():
                sp = t.get("save_path") or ""
                cp = t.get("content_path") or ""
                if not under(sp, old):
                    continue          # 根在旧目录之上：setLocation 改不了它的根目录名，留给人
                if not (cp and Path(cp).exists()):
                    continue          # 死链 / 没有元数据：setLocation 搬不动它
                movable.append(t)

            if self.dry_run:
                # 以前 setLocation 在 dry-run 判断之前就发出去了：预演也真的搬了种子。
                remaining = sum(1 for p in old.rglob("*")
                                if p.is_file() and fold(p) not in claimed
                                and not self._is_junk(p.name))
                results.append({"old": old.name, "new": new.name,
                                "would_move_via_qbit": len(movable),
                                "would_move_via_fs": remaining})
                continue

            via_qbit, failed = 0, []
            for t in movable:
                try:
                    self.ctx.qbit.set_location(
                        [t["hash"]], repath(t["save_path"], old, new))
                    via_qbit += 1
                except Exception as e:
                    failed.append(f"{t['hash'][:8]}：{type(e).__name__}: {e}")
            if movable:
                self._claims().invalidate()     # 搬过种子：下一对要重新问占用
            if failed:
                # 以前吞掉异常接着 _merge_tree：qBit 仍记着旧 save_path，文件却被
                # 文件系统搬去了新目录——种子失联。这一对停手，交给人。
                results.append({"old": old.name, "new": new.name,
                                "moved_via_qbit": via_qbit, "moved_via_fs": 0,
                                "stranded": 0, "old_removed": False,
                                "error": (f"{len(failed)} 个种子 setLocation 失败，这一对不做"
                                          f"文件系统合并：{failed[0]}")})
                continue

            moved, stranded = self._merge_tree(old, new, skip=claimed)
            results.append({"old": old.name, "new": new.name,
                            "moved_via_qbit": via_qbit, "moved_via_fs": moved,
                            "stranded": stranded,
                            "left_for_torrents": sum(1 for p in claimed.values() if p.exists()),
                            "old_removed": not old.exists()})

        return {"pairs": len(pairs), "detail": results, "refused": ""}

    def list_runs(self) -> list[dict]:
        """列出历史 run，供选择回退哪一次。"""
        if not self.cfg.audit_log.exists():
            return []
        runs: dict[str, dict] = {}
        for line in self.cfg.audit_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = rec.get("run_id")
            if not rid or rec.get("dry_run"):
                continue
            r = runs.setdefault(rid, {"run_id": rid, "ts": rec.get("ts", ""),
                                      "applied": 0, "undoable": 0, "kinds": set()})
            if rec.get("status") == "applied":
                r["applied"] += 1
                if rec.get("undo"):
                    r["undoable"] += 1
                r["kinds"].add(rec.get("kind", ""))
            if rec.get("status") == "rollback":
                r["rolled_back"] = True
        out = []
        for r in runs.values():
            r["kinds"] = sorted(k for k in r["kinds"] if k)
            out.append(r)
        return sorted(out, key=lambda r: r["ts"])
