"""一轮之内收敛：扫描 → 诊断 → 执行，重复到不动点（最多 `MAX_ITERATIONS` 次，默认 3）。

**为什么要迭代。** 一轮 `run` 以前是"先全量诊断、再统一执行"：执行里做成的事，要等**下一轮**（6 小时后）的诊断
才看得见。前后依赖的两步因此永远隔一轮（runloop 调研 §7A / §8a）：

- AutoBangumi 的重复版本落在 `Bangumi` 分类：这一轮只能交接分类（recategorize），判重要等分类交接完才敢做
  （所有权边界，AGENTS.md 第 6 条），赢家的改名撞「集位被占」——判重与改名都要等下一轮（6 小时后）。生产上最近 3 批没有隔离
  却有「集位被占」的，同一批里都有一次 recategorize。
- 抓取之后的改名：元数据没在等待时限内到、或种子不止一个视频时，改名"下一轮会补"。
- relink（op 1）、目录改名（op 8）之后，下游规则看到的都是改之前的样子。

现在一轮 `run` 在同一个进程里重复"扫描 → 诊断 → 执行"，只要上一次迭代**做成了新的动作**就再来一次。

**一轮一个执行器**（`Executor`）：一个批次 ID（整轮仍是一个回退单元）、删除配额跨迭代累计（每次迭代新建执行器
等于把 `MAX_DELETE_PER_RUN=50` 变成 N×50）、`_grabbed`（写 sidecar 时并回本轮抓的集）、本轮摘掉的种子。按路径记的
"本批次已隔离"与占用索引每次迭代清掉（`Executor.new_iteration`）：新的扫描已经反映了它们，同一个路径上此刻可能已是
改名过来的赢家。**每次迭代一份新的扫描**（`build_state` 开头清种子文件列表与 season_offsets 的进程内缓存，testinfra B3、critic §3.2），
TMDB 身份照常解析（不能像演进重扫那样 `resolve_tmdb=False`：标题退回目录名、同一轮里来回改名，runloop B4）。

**不重试、不打架。** 同一轮里一个动作（按"做什么 + 对谁"认，`key_of`）只试一次：已执行、失败、未确认、被闸拦下
（删除关口、配额、演进规则、坏 sidecar……）的，后面的迭代不再试——否则每次迭代都会再撞一次同样的 404（testinfra B2）、同样的拒绝（critic §3.12）。
只有"此刻被别的东西挡着"、而本轮别的动作可能把它挪开的跳过（`RETRYABLE`：集位被占、目标文件归另一个活种子）
后面的迭代再试：判重在下一次迭代腾出集位，赢家就改得成。一个动作要是会**撤销**本轮已经执行的动作（改名 X→Y 之后
又要 Y→X、分类 A→B→A、刚抓的种子又要摘掉、刚摘的种子又要抓回来），拒绝它、记一条 skipped 审计，报一条
`oscillation` 发现：那是两条规则在打架，来回改只会让 Jellyfin 的刮削记录跟着乱（runloop B5 / LAT-04 那种翻来覆去，只是压进了一轮）。

**停在哪。** 某次迭代没有做成任何新动作 = 不动点。到了上限还在做新动作 = 到顶：再扫描、诊断一次（不执行），把
下一次迭代会做的列成"待做"（健康报告 warn `loop_cap`：到顶多半说明有规则在拉锯）。任何一次迭代的扫描读 qBittorrent
不完整，执行器照旧整批拒绝（`Executor.qbit_blocker`），循环立刻停，调用方按原来的拒绝语义收尾。预演只跑一次：动作
都没执行，第二次看到的与第一次相同。上一次迭代搬过存储（目录改名、relocate：qBittorrent 的 setLocation 是异步的）
的种子，下一次扫描之前先等它们搬完（`SETTLE_TIMEOUT_S`）；等不到就停在这里（`MOVING`），不在"一半在旧目录、一半在
新目录"的视图上诊断，剩下的下一轮做。

**每次迭代花多少。** 第二次迭代起网络全走缓存：TMDB 身份与标题按 id 缓存 30 天、搜不到的负缓存 24 小时、分集表
6 小时 / 7 天（`cache.season_episodes`），番组页与 RSS 1 小时，Mikan 搜索 7 天；本地动作改变不了其中任何一样。剩下
的是 qBittorrent 的种子文件列表（必须是此刻的）与磁盘。所以每次迭代都跑全部检测器，不按迭代挑——生产规模下第二次
迭代约 2–3 秒（`.agents/notes/implemented/architecture/2026-09-27-converge-within-a-run.md`）。

**`media-agent grab` 也用它。** `run(ctx, reg, ex, scan=…, select=…)` 不认识 `cmd_run`：抓取模式（`grabmode.run`）传只有
抓取相关检测器的 `Registry` 与 `select=only(…, rename=…, trash=…)`，同样得到一轮一个执行器、不重试、反向拒绝、到顶报待做。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import audit as auditlog
from .kernel import Finding

# 停下来的原因
FIXED_POINT = "fixed_point"      # 某次迭代没有做成任何新动作（到顶之后的收尾诊断也没有待做的，同样算）
CAP = "cap"                      # 到了上限，下一次迭代还有要做的
REFUSED = "refused"              # 某次迭代的扫描读 qBittorrent 不完整，执行器整批拒绝
DRY_RUN = "dry_run"              # 预演：只跑一次
CRASHED = "crashed"              # 半路抛了异常（调用方记下；`run` 自己不接住）
MOVING = "moving"                # 上一次迭代搬过存储的种子，qBittorrent 在时限内没搬完：不在半搬的视图上再诊断

# 搬存储（setLocation）的动作。qBittorrent 5.2.3 对有元数据的种子是排一个异步搬运任务：搬完之前 `state` 是 `moving`、
# `save_path` 不变（`Executor._location_landed`）。下一次迭代的扫描要等它们搬完（`_wait_settled`）。
MOVES = ("rename_show_dir", "relocate")
SETTLE_TIMEOUT_S = 60.0          # 同一个卷里搬是改名，一两秒；等满还在搬（跨卷拷贝、qBittorrent 卡住）就停在这一次迭代
SETTLE_POLL_S = 1.0
_sleep = time.sleep              # 测试替换它（FakeQbit 的异步搬运由 `drain()` 完成）

# 这些跳过是"此刻被别的东西挡着"，本轮别的动作可能把它挪开：后面的迭代再试。其余跳过（删除关口、配额、演进规则、
# 坏 sidecar、目标目录要人合并、种子已不在……）同一轮里再试结果也一样，按"已试过"处理。
RETRYABLE = (
    "集位被占",                   # 改名：判重下一次迭代腾出集位（`Executor._occupied`）
    "目标文件已归另一个活种子",     # relink：占着的那个种子可能下一次迭代被判重 / 死种处置摘掉
)

# 各动作"对谁"做（`key_of`）：同一个目标同一种动作，一轮只试一次。没列出的动作按全部参数认。
_TARGET: dict[str, tuple[str, ...]] = {
    "rename": ("path", "torrent_hash"),
    "trash": ("path", "torrent_hash"),
    "recategorize": ("torrent_hash",),
    "retag": ("torrent_hash", "tags"),
    "drop_torrent": ("torrent_hash",),
    "relink_torrent": ("torrent_hash",),
    "relocate": ("torrent_hash",),
    "delete_category": ("category",),
    "rename_show_dir": ("path",),
    "write_nfo": ("path",),
    "write_sidecar": ("show_dir",),
    "pin_tmdb": ("show_dir",),
    "adopt_episode_offset": ("show_dir", "season"),
    "subscribe_season": ("show_dir", "season"),
    "create_show_dir": ("show_dir",),
    "grab_episode": ("show_dir", "season", "episode"),
    "fix_title_aliases": ("bangumi_id",),
    "repoint_rss": ("bangumi_id",),
}


def only(*ops: str, **guards: Callable[[Finding], bool]) -> Callable[[Finding], bool]:
    """`run(select=…)` 用：只做这几种动作，别的发现照样诊断出来、不执行。`guards`（动作名 → 判据）：这种动作也做，
    但只做判据说是的那些——`media-agent grab` 的改名只改本项目抓的种子、判重只判集位里有本项目抓的那一份
    （`grabmode.Scope`）。"""
    wanted = frozenset(ops) | frozenset(guards)

    def pick(f: Finding) -> bool:
        if f.action is None or f.action.op not in wanted:
            return False
        guard = guards.get(f.action.op)
        return guard is None or bool(guard(f))
    return pick


def key_of(op: str, args: dict | None) -> tuple:
    """一个动作的身份：做什么（op）+ 对谁（目标的几个参数）。同一个路径上换了一个种子的文件，是另一个目标。"""
    args = args or {}
    fields = _TARGET.get(op)
    if fields is None:
        return (op, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
    return (op, *(str(args.get(k) if args.get(k) is not None else "") for k in fields))


def retryable(rec: dict) -> bool:
    """这条审计记录说的跳过，本轮后面的迭代值不值得再试（见 `RETRYABLE`）。"""
    return (rec.get("status") == auditlog.SKIPPED
            and str(rec.get("reason") or "").startswith(RETRYABLE))


def _grabbed_hash(args: dict) -> str:
    from .ledger import infohash_of_url
    return (infohash_of_url(str(args.get("url") or "")) or "").lower()


def undoes(op: str, args: dict, rec: dict) -> bool:
    """动作 (op, args) 是不是在撤销已执行的 `rec`（按 `rec` 自己记下的逆操作认）。"""
    args = args or {}
    u = rec.get("undo") or {}
    uop = u.get("op")
    h = str(args.get("torrent_hash") or "").lower()
    if op in ("rename", "rename_show_dir") and uop == op:
        return str(args.get("path")) == str(u.get("path")) and args.get("new_name") == u.get("new_name")
    if op == "recategorize" and uop == "recategorize":
        return h == str(u.get("torrent_hash") or "").lower() and args.get("category") == u.get("category")
    if op == "relink_torrent" and uop == "relink_torrent":
        pairs = lambda m: {(x.get("old"), x.get("new")) for x in (m or [])}   # noqa: E731
        return (h == str(u.get("torrent_hash") or "").lower()
                and pairs(args.get("mapping")) == pairs(u.get("mapping")))
    if op in ("drop_torrent", "trash") and uop == "ungrab_episode":
        # 刚抓进来的种子又要摘掉 / 又要把抓的那一集隔离。只作废多文件发布里的**另一个**条目（NCOP、PV：extras-in-library
        # 的 `file_only`）不撤销抓取——以前一律算，报一条 important 的"两条规则在打架"，下一轮又照常清掉（2026-09-27 审查）
        if not (h and h == str(u.get("infohash") or "").lower()):
            return False
        if op == "drop_torrent" or not args.get("file_only"):
            return True
        return _is_grabbed_episode(str(args.get("path") or ""), u)
    if op == "grab_episode":
        # 刚摘掉的种子又要抓回来
        removed = (uop == "readd_torrent"
                   or (uop == "restore_from_trash" and u.get("torrent_record_lost")))
        rh = str((rec.get("args") or {}).get("torrent_hash") or "").lower()
        return removed and bool(rh) and rh == _grabbed_hash(args)
    return False


def _is_grabbed_episode(path: str, u: dict) -> bool:
    """这个文件是不是抓的那一集（`ungrab_episode` 的逆操作记着季、集）：按文件名认——改好名的 `… S01E09.mkv`、还叫
    发布名的 `… - 09 […]` 都认得出；认不出集号的（NCOP、PV、菜单）不是。"""
    from pathlib import PurePath
    from .naming import parse_episode

    season, ep = parse_episode(PurePath(path).name)
    try:
        want = (int(u.get("season")), int(u.get("episode")))
    except (TypeError, ValueError):
        return True                                   # 逆操作没记集位（旧记录）：按以前的口径算撤销
    return ep == want[1] and season in (None, want[0])


@dataclass
class Iteration:
    """一次迭代：扫描、诊断、执行各花多少，诊断出什么，执行结果（只算这一次迭代新写的审计）。"""
    n: int
    findings: int = 0
    actionable: int = 0          # 带动作、且在这一轮要做的范围里（`select`）
    attempted: int = 0           # 交给执行器的
    memo: int = 0                # 本轮已经试过、不再试的
    reversed: int = 0            # 会撤销本轮已执行的动作：拒绝
    deferred: int = 0            # 刚动过的种子：这一轮不按死种摘（`TOUCHING`）
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    unknown: int = 0
    refused: str = ""
    degraded: bool = False
    final: bool = False          # 到顶之后的收尾诊断：不执行，只看还剩什么
    scan_s: float = 0.0
    diagnose_s: float = 0.0
    apply_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self, max_n: int) -> str:
        head = "收尾诊断（到顶之后，不执行）" if self.final else f"迭代 {self.n}/{max_n}"
        s = (f"  ↻ {head}：扫描 {self.scan_s:.1f}s · 诊断 {self.diagnose_s:.1f}s（{self.findings} 个问题，"
             f"可执行 {self.actionable}）")
        if self.final:
            return s + (f" · 待做 {self.attempted}" if self.attempted else " · 没有待做的")
        if self.refused:
            return s + f" · ⛔ 整批拒绝：{self.refused[:80]}"
        s += (f" · 执行 {self.applied} · 跳过 {self.skipped} · 失败 {self.failed}"
              + (f" · 未确认 {self.unknown}" if self.unknown else "")
              + (f" · 本轮已试过 {self.memo}" if self.memo else "")
              + (f" · 反向拒绝 {self.reversed}" if self.reversed else "")
              + (f" · 暂缓 {self.deferred}" if self.deferred else "")
              + f"（{self.apply_s:.1f}s）")
        return s


@dataclass
class Outcome:
    """整轮迭代的结果。`findings` 是**最后一次**诊断的（到顶时是收尾诊断的）加上本轮的 `oscillation`——发现历史、
    卡住检测、健康报告都只看它；各次迭代的数在 `iterations` 里。"""
    max_iterations: int
    iterations: list[Iteration] = field(default_factory=list)
    stop: str = ""
    refused: str = ""
    # 到顶之后的收尾诊断读 qBittorrent 不完整：待做的列不出来（空），原因在这里
    final_degraded: str = ""
    # 停在 `MOVING`：上一次迭代搬过存储的种子还没搬完（原因）
    unsettled: str = ""
    state: object = None                             # 最后一次扫描的 LibraryState
    findings: list[Finding] = field(default_factory=list)
    oscillations: list[Finding] = field(default_factory=list)
    pending: list[Finding] = field(default_factory=list)
    # 到顶时仍被挡着、最后一次迭代也没碰过挡着它的东西的（「集位被占」）：下一次迭代再试也一样，不算待做
    still_blocked: list[Finding] = field(default_factory=list)
    # 全部迭代里提过的动作（按 `key_of` 去重，先到的留下）：抓取提议了几集之类的"本轮合计"
    proposed: list[Finding] = field(default_factory=list)
    # TMDB 标题稳定闸的决定（`titles.record` 一轮只记一次：迭代之间记了，同一轮就会被数成两轮）
    title_decisions: dict = field(default_factory=dict)
    detector_errors: list[dict] = field(default_factory=list)

    def to_dict(self, limit: int = 20) -> dict:
        def brief(f: Finding) -> dict:
            return {"rule": f.rule, "kind": f.kind, "op": f.action.op if f.action else None,
                    "show": f.show, "path": str(f.path or ""), "summary": str(f.summary)[:160]}
        return {"max": self.max_iterations, "stop": self.stop, "final_degraded": self.final_degraded,
                "unsettled": self.unsettled,
                "iterations": [it.to_dict() for it in self.iterations],
                "pending_count": len(self.pending), "pending": [brief(f) for f in self.pending[:limit]],
                "still_blocked": [brief(f) for f in self.still_blocked[:limit]],
                "oscillations": [{**brief(f), "evidence": f.evidence} for f in self.oscillations[:limit]]}


# 会让种子重新校验 / 搬存储的动作：做完之后它有一阵子"下载中、0 做种、0 可用"——还没连上 peer，不是死了。
# 以前一轮只诊断一次，死种判定隔着 6 小时；迭代时下一次迭代就会按死种摘掉它（critic §3.3）。这一轮暂缓。
TOUCHING = ("relink_torrent", "relocate", "rename_show_dir")


def _touched_hashes(rec: dict) -> set[str]:
    args = rec.get("args") or {}
    if rec.get("op") == "rename_show_dir":
        return {str(h).lower() for h, _sp in (rec.get("undo") or {}).get("torrent_savepaths") or []}
    return {str(args.get("torrent_hash") or "").lower()} - {""}


def _moved_hashes(recs: list[dict]) -> set[str]:
    """这些审计记录里搬过存储的种子（`MOVES`，含半途而止、未确认的目录改名：已受理的那几个照样在搬）。"""
    out: set[str] = set()
    for rec in recs:
        if rec.get("op") not in MOVES or rec.get("status") not in (auditlog.APPLIED, auditlog.UNKNOWN,
                                                                  auditlog.FAILED):
            continue
        u = rec.get("undo") or {}
        if rec.get("op") == "rename_show_dir":
            out |= {str(h).lower() for h in u.get("moved_hashes") or []}
            out |= {str(h).lower() for h, _sp in u.get("torrent_savepaths") or []}
        else:
            out |= {str((rec.get("args") or {}).get("torrent_hash") or "").lower()} - {""}
    return out


def _wait_settled(ctx, hashes: set[str]) -> str:
    """等上一次迭代搬过存储的种子搬完（没有一个还是 `moving`），最多 `SETTLE_TIMEOUT_S` 秒。搬完了返回空串，
    否则返回原因。

    2026-09-27 审查：以前下一次迭代在 `apply` 返回的那一刻就扫描（以前隔 6 小时）。目录改名之后种子还在旧目录里搬，
    新目录里只有 `_merge_tree` 挪过去的纯本地文件、NFO 与档案——扫描把两个目录当成两部番：sidecar-sync 按半搬的视图把
    新目录的 `have` 从 [1, 2, 3] 写成 [3]，任何别的检测器在这份拆开的视图上都可能出错。读不到 qBittorrent 同样当作
    "不知道搬完没有"。"""
    if not hashes:
        return ""
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while True:
        try:
            moving = sorted(str(t.get("hash") or "").lower() for t in ctx.qbit.torrents()
                            if str(t.get("hash") or "").lower() in hashes and t.get("state") == "moving")
        except Exception as e:                      # noqa: BLE001 —— 交给调用方：原因进 Outcome.unsettled、这一轮停在这里
            return f"读不到 qBittorrent，确认不了刚搬过存储的 {len(hashes)} 个种子搬完没有（{type(e).__name__}: {e}）"
        if not moving:
            return ""
        if time.monotonic() >= deadline:
            return (f"qBittorrent 还在搬这一轮改过存储位置的 {len(moving)} 个种子（等了 {SETTLE_TIMEOUT_S:.0f} 秒）："
                    f"{', '.join(h[:8] for h in moving[:5])}——不在半搬的视图上再诊断，剩下的下一轮做")
        _sleep(SETTLE_POLL_S)


class _Guard:
    """一轮之内的"试过了"、"反向"、"刚动过的种子"三道闸（见模块文档）。"""

    def __init__(self, ex):
        self.ex = ex
        self.tried: dict[tuple, str] = {}           # key → 结局（applied / failed / unknown / skipped）
        self.applied_at: list[tuple[int, dict]] = []  # (第几次迭代, 已执行的审计记录)
        self.reversed: set[tuple] = set()           # 拒绝过的（反向 / 暂缓）：后面的迭代不再记一遍
        self.blocked: dict[tuple, tuple[int, dict]] = {}   # 被挡着的（`RETRYABLE`）：最近一次在哪次迭代、那条记录

    def note(self, recs: list[dict], n: int) -> None:
        for rec in recs:
            key = key_of(str(rec.get("op") or ""), rec.get("args"))
            if retryable(rec):
                self.blocked[key] = (n, rec)
                continue
            self.tried.setdefault(key, str(rec.get("status")))
            if rec.get("status") == auditlog.APPLIED:
                self.applied_at.append((n, rec))

    def still_blocked(self, f: Finding, n: int, others: list[Finding]) -> bool:
        """到顶后的收尾诊断：这个动作在最后一次迭代（第 `n` 次）里被挡着，而那次迭代做成的事、其余待做的事都碰不到
        挡着它的东西（占着目标名的文件 / 声明着它的种子）——下一次迭代再试也一样被挡，不算"待做"。

        以前 `RETRYABLE` 的跳过一律列成待做：长期挂着一个被占集位的库（生产审计里「目标文件名已存在」的跳过 2657 次），
        每一轮用满迭代都报 `loop_cap`（"每轮都到顶，多半是有规则在拉锯"），而真正要做的早就做完了（2026-09-27 审查）。
        只看"最后一次迭代也被挡"不够：AB 重复版本那一轮，分类交接之后判重（待做）才腾得出集位——碰得到挡着它的东西的
        （判重要隔离占位的、同一次迭代里排在后面把占位的改走了的改名）仍是待做。"""
        a = f.action
        hit = self.blocked.get(key_of(a.op, a.args)) if a else None
        if hit is None or hit[0] != n:
            return False
        paths, hashes = _blockers(a.args, hit[1])
        touching = [rec.get("args") or {} for m, rec in self.applied_at if m == n]
        touching += [g.action.args for g in others if g is not f and g.action]
        undo_hashes = {str(h).lower() for m, rec in self.applied_at if m == n
                       for h, _sp in (rec.get("undo") or {}).get("torrent_savepaths") or []}
        if undo_hashes & hashes:
            return False
        return not any(str(x.get("path") or "") in paths
                       or str(x.get("torrent_hash") or "").lower() in hashes for x in touching)

    def _touched(self) -> dict[str, tuple[int, dict]]:
        out: dict[str, tuple[int, dict]] = {}
        for m, rec in self.applied_at:
            if rec.get("op") in TOUCHING:
                for h in _touched_hashes(rec):
                    out.setdefault(h, (m, rec))
        return out

    def screen(self, findings: list[Finding], n: int, *, select=None, write: bool = True
               ) -> tuple[list[Finding], int, list[Finding], int]:
        """这一次迭代要交给执行器的、因为本轮试过而跳过的个数、反向的（`oscillation` 发现）、刚动过的种子暂缓的个数。
        `write`：反向与暂缓的写一条 skipped 审计（收尾诊断不执行，不写）。"""
        todo, memo, osc, deferred = [], 0, [], 0
        touched = self._touched()
        for f in findings:
            if not f.action or (select is not None and not select(f)):
                continue
            a = f.action
            key = key_of(a.op, a.args)
            if key in self.reversed:
                memo += 1
                continue
            hit = next(((m, rec) for m, rec in self.applied_at if undoes(a.op, a.args, rec)), None)
            if hit is not None:
                m, rec = hit
                self.reversed.add(key)
                o = _oscillation(f, rec, m, n)
                osc.append(o)
                if write:
                    self.ex.refuse(f, o.evidence["reason"], {"reverses": o.evidence["first"]})
                continue
            hit = (touched.get(str(a.args.get("torrent_hash") or "").lower())
                   if a.op == "drop_torrent" and a.args.get("dead") else None)
            if hit is not None:
                m, rec = hit
                self.reversed.add(key)
                deferred += 1
                if write:
                    self.ex.refuse(f, (
                        f"刚动过的种子：本轮第 {m} 次迭代 [{rec.get('rule')}] 已 {rec.get('op')}（重新校验 / 搬存储），"
                        f"它此刻\"没人做种\"多半只是还没连上 peer——这一轮不按死种摘，下一轮再看"),
                        {"touched_by": {"iteration": m, "rule": rec.get("rule"), "op": rec.get("op"),
                                        "summary": str(rec.get("summary") or "")[:160]}})
                continue
            if key in self.tried:
                memo += 1
                continue
            todo.append(f)
        return todo, memo, osc, deferred


def _blockers(args: dict, rec: dict) -> tuple[set[str], set[str]]:
    """被挡着的动作（`RETRYABLE` 的跳过记录）是被谁挡着的：路径（改名的目标、占用者声明的路径）与种子 hash。"""
    from pathlib import PurePath
    paths: set[str] = set()
    hashes: set[str] = set()
    if args.get("path") and args.get("new_name"):
        paths.add(str(PurePath(str(args["path"])).parent / str(args["new_name"])))

    def walk(x) -> None:
        if isinstance(x, dict):
            if x.get("path"):
                paths.add(str(x["path"]))
            if x.get("hash"):
                hashes.add(str(x["hash"]).lower())
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk([rec.get("claimants"), rec.get("claims")])
    hashes.discard("")
    return paths, hashes


def _oscillation(f: Finding, rec: dict, first_n: int, n: int) -> Finding:
    a = f.action
    first = {"iteration": first_n, "rule": rec.get("rule"), "op": rec.get("op"),
             "summary": str(rec.get("summary") or "")[:160], "args": rec.get("args")}
    second = {"iteration": n, "rule": f.rule, "kind": f.kind, "op": a.op,
              "summary": str(f.summary)[:160], "args": a.args}
    reason = (f"反向动作：会撤销本轮第 {first_n} 次迭代 [{rec.get('rule')}] 已执行的 {rec.get('op')}"
              f"（{str(rec.get('summary') or '')[:60]}）——两条规则在打架，拒绝后一个")
    return Finding(
        rule="run-loop", kind="oscillation", severity="important", classified=True,
        summary=(f"[{f.rule}] 要做的 {a.op}（{str(f.summary)[:60]}）会撤销同一轮里 [{rec.get('rule')}] "
                 f"刚做的 {rec.get('op')}——两条规则在打架，已拒绝后一个；要人看哪条规则的判断不对"),
        show=f.show, path=str(f.path or ""), torrent_hash=f.torrent_hash,
        evidence={"reason": reason, "first": first, "second": second})


# 这一轮刚由模型的选择钉进 sidecar 的 TMDB 身份（`pin_tmdb`）：后面的迭代照 sidecar 认它，但这一轮不按它改名
HOLD_FRESH_PIN = ("TMDB 身份是这一轮刚由模型的选择钉进 sidecar 的（pin_tmdb）：这一轮不按它改名 / 改目录名 / 改分类 / 抓取，"
                  "下一轮起才按它认——不对就改 sidecar 的 tmdb_id")


def _hold_fresh_pins(state, guard: _Guard) -> None:
    """critic N4 的约定："模型选的条目这一轮不用"（`scan._resolve_tmdb`）。以前一轮只诊断一次，钉进去的身份自然要等
    下一轮；迭代时第二次扫描已经照 sidecar 认它了——不拦的话，模型的选择在钉进去的同一轮就改了文件名与目录名，人连看一眼
    `tmdb_pick` 的机会都没有。`naming_hold` 让改名、目录名、分类、NFO、抓取这一轮都不按它做（`tmdb-identity` 报 `naming_held`）。"""
    pinned = {str((rec.get("args") or {}).get("show_dir")) for _, rec in guard.applied_at
              if rec.get("op") == "pin_tmdb"}
    for show in (getattr(state, "shows", None) or []) if pinned else []:
        if str(show.dir_path) in pinned and not show.naming_hold:
            show.naming_hold = HOLD_FRESH_PIN


def run(ctx, reg, ex, *, scan: Callable[[int], object], max_iterations: int,
        select: Callable[[Finding], bool] | None = None,
        on_scan: Callable[[int, object], None] | None = None,
        on_diagnose: Callable[[int, object, list[Finding]], None] | None = None,
        on_iteration: Callable[[Iteration], None] | None = None,
        out: Outcome | None = None) -> Outcome:
    """迭代到不动点。`scan(n)` 返回第 n 次迭代的 LibraryState；`ex` 是这一轮唯一的执行器。
    `out`：调用方自己建的 `Outcome`（边跑边填）——中途抛异常时，已经做完的迭代调用方照样看得到。

    回调（都可省）：`on_scan(n, state)` 扫描之后、诊断之前（`cmd_run` 在第一次迭代挂账本补录、记种子数基线）；
    `on_diagnose(n, state, findings)` 执行之前；`on_iteration(it)` 每次迭代（含收尾诊断）结束时。"""
    max_iterations = max(1, int(max_iterations))
    if out is None:
        out = Outcome(max_iterations=max_iterations)
    out.max_iterations = max_iterations
    guard = _Guard(ex)
    seen: set[tuple] = set()
    errors: dict[tuple, dict] = {}
    n = 0

    def diagnose(it: Iteration):
        t0 = time.monotonic()
        state = scan(it.n)
        _hold_fresh_pins(state, guard)
        it.scan_s = round(time.monotonic() - t0, 2)
        it.degraded = bool(getattr(state, "qbit_errors", None))
        if on_scan:
            on_scan(it.n, state)
        t1 = time.monotonic()
        findings = reg.run_all(ctx, state)
        it.diagnose_s = round(time.monotonic() - t1, 2)
        for e in getattr(reg, "errors", None) or []:
            errors.setdefault((e.get("rule"), e.get("error")), e)
        out.title_decisions.update(getattr(state, "title_decisions", None) or {})
        it.findings = len(findings)
        it.actionable = sum(1 for f in findings if f.action and (select is None or select(f)))
        for f in findings:
            if f.action and (select is None or select(f)):
                k = key_of(f.action.op, f.action.args)
                if k not in seen:
                    seen.add(k)
                    out.proposed.append(f)
        out.state, out.findings = state, findings
        return state, findings

    moved: set[str] = set()

    def settled() -> bool:
        """下一次扫描之前：上一次迭代搬过存储的种子搬完了没有。没搬完就停在这里（`MOVING`）。"""
        why = _wait_settled(ctx, moved)
        if why:
            out.stop, out.unsettled = MOVING, why
        return not why

    while n < max_iterations:
        n += 1
        if n > 1:
            ex.new_iteration()
            if not settled():
                break
        it = Iteration(n)
        state, findings = diagnose(it)
        if on_diagnose:
            on_diagnose(n, state, findings)
        before = {k: len(getattr(ex.report, k)) for k in ("applied", "skipped", "failed", "unknown")}
        todo: list[Finding] = []
        if not ex.qbit_blocker():
            # 读不全的一轮什么都不做（下面 `apply` 整批拒绝），反向动作也不必记
            todo, it.memo, osc, it.deferred = guard.screen(findings, n, select=select)
            it.reversed = len(osc)
            out.oscillations += osc
        it.attempted = len(todo)
        t2 = time.monotonic()
        ex.apply(todo)
        it.apply_s = round(time.monotonic() - t2, 2)
        new = {k: getattr(ex.report, k)[before[k]:] for k in before}
        it.applied, it.skipped = len(new["applied"]), len(new["skipped"])
        it.failed, it.unknown = len(new["failed"]), len(new["unknown"])
        out.iterations.append(it)
        if ex.report.refused:
            it.refused = out.refused = ex.report.refused
        else:
            guard.note([r for recs in new.values() for r in recs], n)
            moved = _moved_hashes([r for recs in new.values() for r in recs])
        if on_iteration:
            on_iteration(it)
        if it.refused:
            out.stop = REFUSED
            break
        if ex.dry_run:
            out.stop = DRY_RUN
            break
        if not it.applied:
            out.stop = FIXED_POINT
            break
    else:
        # 到顶：上一次迭代还做成了新动作。再看一次（不执行），把下一次迭代会做的列成待做——同样先等搬完
        ex.new_iteration()
        if settled():
            it = Iteration(n + 1, final=True)
            state, findings = diagnose(it)
            errs = list(getattr(state, "qbit_errors", None) or [])
            if errs:
                # 残缺的种子视图里"还要做什么"不可信：待做的不列，原因交给调用方（`cmd_run` 按重扫读不全报 critical）
                out.final_degraded = errs[0]
                out.stop = CAP
            else:
                todo, it.memo, osc, it.deferred = guard.screen(findings, n + 1, select=select, write=False)
                out.still_blocked = [f for f in todo if guard.still_blocked(f, n, todo)]
                todo = [f for f in todo if f not in out.still_blocked]
                it.attempted, it.reversed = len(todo), len(osc)
                out.oscillations += osc
                out.pending = todo
                out.stop = CAP if todo else FIXED_POINT
            out.iterations.append(it)
            if on_iteration:
                on_iteration(it)

    out.detector_errors = list(errors.values())
    fps = set()
    for o in out.oscillations:
        k = key_of(o.evidence["second"]["op"], o.evidence["second"]["args"])
        if k not in fps:
            fps.add(k)
            out.findings = [*out.findings, o]
    return out
