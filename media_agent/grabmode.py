"""抓取模式（`media-agent grab`，launchd 每 30 分钟）：只补缺的集，并把刚抓的那一集收尾。

**为什么要单独一个模式。** AutoBangumi 每 15 分钟拉一次 RSS、60 秒改一次名；`run` 每 6 小时一轮。AB 退役之后，新集最坏
晚 6 小时才抓，刚抓的多视频 / 元数据没及时到的发布要等 6 小时才改名（ab 调研 §5.1 / §5.2）。可 `run` 是完整的治理：扫全库、
跑全部规则、处置隔离区——每 30 分钟跑一遍既没必要，也让每一条规则的动作频率翻 12 倍。

**与 `run` 同一套机器。** `converge.run`：一轮一个执行器（一个批次 ID、删除配额、`_grabbed`）、迭代到不动点、试过的不再试、
撤销本轮动作的拒绝（`oscillation`）。只是检测器少（`registry()`），动作挑着做（`Scope.select`，`converge.only`）：

| 检测器 | 做的动作 | 为什么在抓取模式里 |
|---|---|---|
| `ab-adoption` | `create_show_dir` / `subscribe_season` / `adopt_episode_offset` | AB 里刚订的新番 30 分钟之内开始抓，不等 6 小时的 `run` |
| `episode-available`（`replace_dead=False`） | `grab_episode` | 本职。**不换源**：旧种子死了换个发布，要同一批摘掉旧种子（dead-torrent），不在这里做；放行了就是两个种子抢一个集位 |
| `duplicate-episode` | `trash`，只在集位里有本项目抓的那一份时 | 抓取补上的集位与 AB（还没退役时）的那份重复：判重清走输家、赢家才拿得到集位名——抓取的收尾。删除照样过删除关口（I1–I4）与配额 |
| `unrenamed-file` | `rename`，只改本项目抓的种子的文件 | 抓取当场的改名（`_rename_grabbed`）等不到元数据、或多视频的发布，要等改名规则；AB 的文件它自己 60 秒就改 |
| `category-consolidation`（只在订阅模式） | `recategorize`，只把 `Bangumi` / `BangumiCollection` 里的交接到剧名分类 | AB 不再改名：订阅那一刻它补的集当场交接、改名、判重（见下） |

"本项目抓的" = 种子钉着 `ma:SxxEyy`（抓取加种时打的），或出处账本里有这个种子的抓取行（`Row.grabbed`）——按**每次迭代
的扫描**认（`Scope.observe`），这一轮刚抓的下一次迭代就算。

**订阅模式**（`AB_MODE=subscription`，`abmode`）：AB 的改名线程停了，人在 AB 里订阅的那一刻它把已发布的集补进 `Bangumi`
分类，之后再没人改名——这些也算"本项目的"：多一条 `category-consolidation`，只做把 `Bangumi` / `BangumiCollection` 里的
种子交接到剧名分类（`recategorize`；别的分类碎片、删空分类仍是 `run` 的事），这一轮见过在 AB 分类里的种子交接之后照样
改名、判重（`Scope.handed`）。以前要等 6 小时的 `run`，而 AB 60 秒就改好了。

**不做的**（留给 6 小时的 `run`）：换源与摘死种、标题对齐 / 目录改名、分类交接、NFO、特典、sidecar 同步、新一季登记、
出处账本的补录、标题稳定闸的计数、隔离区处置、发现历史（抓取每 30 分钟一份会把 `run` 的快照挤出保留窗口，卡住检测只数
`run`）、演进。健康报告写 `state/health/grab/`（`cmd: grab`），通知只为抓取相关的事发（`notify.maybe_send(scope="grab")`）。
"""
from __future__ import annotations

from typing import Callable

from . import abmode, converge
from .kernel import Finding, Registry
from .naming import parse_pin

# launchd 的 `StartInterval`（deploy/com.zihan.media-agent-grab.plist）：番组页 feed 的缓存（`cache.FEED_TTL`）必须比它短，
# 否则每一次抓取看到的都是上一次拉的 feed——30 分钟的节奏就白搭了
GRAB_INTERVAL_S = 1800

# 抓取模式里做的动作（改名与判重另有判据，见 `Scope`）
ADOPT_OPS = ("create_show_dir", "subscribe_season", "adopt_episode_offset")


def _handover(cfg) -> bool:
    """订阅模式下抓取接手 AB 分类里的种子（模块文档）。"""
    return cfg is not None and not abmode.ab_renames(cfg)


def registry(cfg=None) -> Registry:
    """抓取模式的检测器（见模块文档的表）。顺序与 `register_builtins` 相同：接手订阅在前，判重在改名之前。
    订阅模式（`cfg.ab_mode`）多一条分类交接。"""
    from .plugins.adopt import AbAdoptionDetector
    from .plugins.builtin import CategoryConsolidationDetector, DuplicateEpisodeDetector, UnrenamedDetector
    from .plugins.grab import EpisodeAvailableDetector

    reg = Registry()
    detectors = [AbAdoptionDetector(), EpisodeAvailableDetector(replace_dead=False), DuplicateEpisodeDetector(),
                 UnrenamedDetector()]
    if _handover(cfg):
        detectors.append(CategoryConsolidationDetector())
    for d in detectors:
        reg.register(d)
    return reg


class Scope:
    """"本项目抓的"种子（见模块文档），每次迭代的扫描之后更新（`observe`）。`cfg` 给了、而且是订阅模式：AB 分类里的
    种子也算（`handed`：这一轮见过的，交接到剧名分类之后照样算）。"""

    def __init__(self, cfg=None) -> None:
        self.grabbed: set[str] = set()
        self.handover = _handover(cfg)
        self.handed: set[str] = set()

    def observe(self, state) -> None:
        hashes = {str(t.get("hash") or "").lower() for t in state.torrents or []
                  if parse_pin(t.get("tags") or "")}
        hashes |= {str(h).lower() for h, row in (state.ledger_rows or {}).items()
                   if row.active and row.grabbed}
        if self.handover:
            self.handed |= {str(t.get("hash") or "").lower() for t in state.torrents or []
                            if (t.get("category") or "") in abmode.AB_CATEGORIES}
        self.grabbed = (hashes | self.handed) - {""}

    def recategorize(self, f: Finding) -> bool:
        """订阅模式下只做 AB 分类的交接：`Bangumi` / `BangumiCollection` → 剧名分类。"""
        return (self.handover and f.rule == "category-consolidation"
                and (f.evidence or {}).get("current") in abmode.AB_CATEGORIES)

    def rename(self, f: Finding) -> bool:
        """改名只改本项目抓的种子的文件。"""
        return str(f.torrent_hash or "").lower() in self.grabbed

    def trash(self, f: Finding) -> bool:
        """判重只在集位里有本项目抓的那一份时做（它是保留方或输家都算：都是抓取补上的集位的收尾）。"""
        if f.rule != "duplicate-episode" or f.action is None:
            return False
        a = f.action.args or {}
        hashes = {str(x or "").lower() for x in (f.torrent_hash, a.get("torrent_hash"), a.get("keep_hash"))}
        return bool((hashes - {""}) & self.grabbed)

    @property
    def select(self) -> Callable[[Finding], bool]:
        return converge.only(*ADOPT_OPS, "grab_episode", rename=self.rename, trash=self.trash,
                             recategorize=self.recategorize)

    def relevant(self, f: Finding) -> bool:
        """给人看的（输出、健康报告的"发现"）：要做的动作，加上抓取与订阅接手自己的发现（找不到番组页、编号对不上……）。
        判重 / 改名规则对别的文件的发现是 `run` 的事，每 30 分钟印一遍只是噪音。"""
        return self.select(f) or f.rule in ("episode-available", "ab-adoption")


def run(ctx, ex, *, scan, max_iterations: int, scope: Scope | None = None, on_scan=None, on_diagnose=None,
        on_iteration=None, out: converge.Outcome | None = None) -> converge.Outcome:
    """抓取模式的一轮：`converge.run` 配上抓取模式的检测器与挑法。`scope` 给了就用它（调用方要按它筛输出）。"""
    scope = scope or Scope(ctx.config)

    def observed(n, state) -> None:
        scope.observe(state)
        if on_scan:
            on_scan(n, state)

    return converge.run(ctx, registry(ctx.config), ex, scan=scan, max_iterations=max_iterations, select=scope.select,
                        on_scan=observed, on_diagnose=on_diagnose, on_iteration=on_iteration, out=out)
