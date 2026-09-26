"""LibraryBuilder：几行代码声明一个媒体库现场，然后离线跑 扫描 → 诊断 → 执行。

    lib.show("尼古喵喵").season(1).single(
        "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv",
        size=593_601_176, probe=video("hevc", subs=["chi 简体中文", "chi 繁體中文"]))
    c = lib.cycle()                       # 新 Context → build_state → run_all → Executor.apply
    assert c.applied("rename")

设计要点（每一条都对应一个会让测试失真的坑）：

- **永远不走 `cli.build_context`**：它会真的 HTTP 登录 qBittorrent / AutoBangumi。
  这里直接 `Context(cfg, qbit=FakeQbit, …)`。
- **Config 直接构造**，`PROJECT_ROOT` 由 conftest 打到临时目录，于是
  `state/`（审计、隔离区、cache.sqlite3）全落在测试自己的 tmp 里。
- **每轮一个新 Context**：测试模拟的是"launchd 每轮起一个新进程"，所以 `cycle()`
  每次都新建 Context，并清掉 `probe._CACHE` / `builtin._OFFSET_CACHE` 这类进程级缓存。
  （`build_state` 自己也会在每次扫描开头清 `ctx._tfile_cache` 与 `_OFFSET_CACHE`——
  B3 已修，见 `tests/test_rescan_freshness.py`；同 ctx 重扫的行为由那里单独覆盖。）
- **run_id 显式给**（`t001`、`t002`…）：断言里好认。执行器的默认 run_id
  带毫秒与 pid（`actions.new_run_id`），不会撞，只是测试里没法预先写出来。
- **文件是稀疏的**：`stat` 报的是声明的逻辑大小，创建瞬时完成；文件头带身份，
  FakeProbe 与 `content_digest` 都认它。
- **日期一律相对今天**（`tmdb.weekly` / `web.days_ago`）：代码直接调 `date.today()`。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from media_agent import converge
from media_agent import probe as probe_mod
from media_agent import sidecar as sc_mod
from media_agent.actions import ExecReport, Executor
from media_agent.config import Config
from media_agent.kernel import Context, Finding, LibraryState, Registry
from media_agent.plugins import builtin as builtin_mod
from media_agent.plugins import subscription as subscription_mod
from media_agent.plugins import register_builtins
from media_agent.scan import build_state

from .ab import FakeAB, insert_bangumi, insert_rows, make_ab_db, make_docker_stub
from .llm import FakeLLM
from .probe import FakeProbe, ProbeSpec
from .qbit import FakeQbit
from .tmdb import FakeTMDB
from .torrentfile import read_ident, write_sparse
from .web import FakeWeb, MikanItem

DEFAULT_VIDEO_SIZE = 600_000_000


def reset_process_caches() -> None:
    """清掉进程级缓存，模拟"新起一个进程"。"""
    probe_mod._CACHE.clear()
    builtin_mod._OFFSET_CACHE.clear()
    subscription_mod._FETCH_FAILED.clear()


@dataclass
class TorrentHandle:
    """builder 造出来的一个种子。"""
    lib: "LibraryBuilder"
    hash: str
    name: str
    save_path: Path
    files: dict[str, int]                          # 条目名（相对 save_path）→ 字节数

    @property
    def paths(self) -> list[Path]:
        return [self.save_path / n for n in self.files]

    @property
    def path(self) -> Path:
        """单文件种子的文件路径（按**加种时**的条目名；改名后请查 `current_paths()`）。"""
        assert len(self.files) == 1, "多文件种子请用 .paths"
        return self.paths[0]

    def view(self) -> dict:
        return self.lib.qbit.torrent(self.hash)

    def current_paths(self) -> list[Path]:
        """qBittorrent 此刻认为的文件路径（跟着 renameFile / setLocation 走）。"""
        t = self.lib.qbit.raw(self.hash)
        return [Path(t["save_path"]) / f["name"] for f in t["_files"]]

    def complete(self) -> None:
        self.lib.qbit.complete(self.hash)


class DirBuilder:
    """一个会被当作种子 save_path 的目录（`Season N`、或任意子目录）。"""

    def __init__(self, show: "ShowBuilder", path: Path):
        self.show = show
        self.lib = show.lib
        self.path = path
        path.mkdir(parents=True, exist_ok=True)

    def local(self, filename: str, size: int = DEFAULT_VIDEO_SIZE, *,
              probe: ProbeSpec | None = None, same_content_as: Path | None = None) -> Path:
        """只在磁盘上、没有种子的文件。"""
        return self.lib._write(self.path / filename, size, probe=probe,
                               same_content_as=same_content_as)

    def single(self, filename: str, size: int = DEFAULT_VIDEO_SIZE, *,
               name: str | None = None, probe: ProbeSpec | None = None,
               **kw) -> TorrentHandle:
        """单文件种子。`name` 是种子显示名，默认等于 `filename`（qBit 对单文件种子
        的 name 就是发布文件名、带扩展名；改过名的文件请显式给原发布名）。"""
        return self.torrent({filename: size}, name=name or filename, layout="single",
                            probe={filename: probe} if probe else None, **kw)

    def torrent(self, files: dict[str, int], *, name: str, layout: str = "original",
                hash: str | None = None, probe: ProbeSpec | dict | None = None,
                progress: float = 1.0, state: str | None = None,
                category: str | None = None, tags: str = "",
                added_hours_ago: float = 24, active_hours_ago: float | None = None,
                seeds: int = 0,
                num_complete: int | None = None, availability: float | None = None,
                on_disk: bool = True, priorities: dict[str, int] | None = None,
                same_content_as: dict[str, Path] | None = None) -> TorrentHandle:
        """多文件种子（或 `layout="single"` 的单文件）。

        - `files`：相对**种子根目录**的路径 → 字节数。
        - `layout`：`"original"`（条目 = `name/相对路径`，content_path = 根目录）、
          `"nosub"`（条目 = 相对路径，content_path = save_path，即本目录）、`"single"`。
        - `category` 默认是剧目录名（= 交接完成后的规范分类，避免
          category-consolidation 在每个测试里冒出噪音）。
        - `progress < 1` 时磁盘上写的是 `<文件>.!qB`；`progress == 0` 不落盘。
        - `probe`：单个 ProbeSpec（套给所有视频）或 `{相对路径: ProbeSpec}`。
        - `priorities`：`{相对路径: 0}` 表示已设为不下载（文件仍在盘上）。
        - `active_hours_ago`：最后一次收发数据距今几小时（`last_activity`）；
          不给则等于加入时间（从没传过数据）。
        """
        assert layout in ("original", "nosub", "single")
        if layout == "original":
            entries = {f"{name}/{rel}": s for rel, s in files.items()}
        else:
            entries = dict(files)
        rel_of = dict(zip(entries, files, strict=True))   # 条目名 → 调用方给的相对路径
        h = (hash or self.lib._next_hash(f"{self.path}/{name}")).lower()
        pri = {e: (priorities or {}).get(rel_of[e], 1) for e in entries}
        self.lib.qbit.seed(
            h, name=name, save_path=self.path, files=entries, progress=progress,
            state=state, category=self.show.name if category is None else category,
            tags=tags, added_on=time.time() - added_hours_ago * 3600,
            last_activity=(None if active_hours_ago is None
                           else time.time() - active_hours_ago * 3600),
            num_seeds=seeds, num_complete=num_complete, availability=availability,
            priorities={e: p for e, p in pri.items() if p != 1})
        if on_disk and progress > 0:
            for i, (entry, size) in enumerate(entries.items()):
                rel = rel_of[entry]
                spec = probe.get(rel) if isinstance(probe, dict) else probe
                target = self.path / entry
                if progress < 1:
                    target = Path(str(target) + ".!qB")
                self.lib._write(target, size, probe=spec, ident=f"{h}:{i}",
                                same_content_as=(same_content_as or {}).get(rel))
        return TorrentHandle(self.lib, h, name, self.path, entries)


class ShowBuilder:
    def __init__(self, lib: "LibraryBuilder", name: str):
        self.lib = lib
        self.name = name
        self.path = lib.media_root / name
        self.path.mkdir(parents=True, exist_ok=True)

    def season(self, n: int) -> DirBuilder:
        return DirBuilder(self, self.path / f"Season {n}")

    def folder(self, rel: str) -> DirBuilder:
        """任意子目录，如 `"Season 1/.extras"`、`"Specials"`，或 `""`（剧目录本身）。"""
        return DirBuilder(self, self.path / rel if rel else self.path)

    def local(self, rel: str, size: int = DEFAULT_VIDEO_SIZE, **kw) -> Path:
        return self.lib._write(self.path / rel, size, **kw)

    def sidecar(self, **fields) -> sc_mod.Sidecar:
        """写 `.media-agent.json`（在已有内容上覆盖给出的字段）。"""
        sc = sc_mod.load(self.path)
        for k, v in fields.items():
            assert k in sc_mod.Sidecar.__dataclass_fields__, f"Sidecar 没有字段 {k}"
            setattr(sc, k, v)
        sc_mod.save(self.path, sc)
        return sc

    def tmdb(self, tmdb_id: int, *, title: str | None = None, original: str = "",
             seasons: dict | None = None) -> "ShowBuilder":
        """登记 TMDB 条目；目录名自动成为查询词（`scan._resolve_tmdb` 先搜目录名）。"""
        self.lib.tmdb.add_show(tmdb_id, title or self.name, original=original,
                               seasons=seasons, queries=[self.name])
        return self

    def bangumi(self, id: int, *, title_raw: str, season: int = 1, **kw) -> dict:
        """AutoBangumi 订阅行，save_path 默认 `<剧>/Season <season>`。"""
        kw.setdefault("official_title", self.name)
        kw.setdefault("save_path", str(self.path / f"Season {season}"))
        return self.lib.bangumi(id=id, title_raw=title_raw, season=season, **kw)


@dataclass
class Cycle:
    """一轮 扫描 → 诊断 → 执行 的结果。"""
    run_id: str
    ctx: Context
    state: LibraryState
    findings: list[Finding]
    report: ExecReport

    def kinds(self) -> list[str]:
        return sorted(f.kind for f in self.findings)

    def actions(self, op: str | None = None) -> list[Finding]:
        return [f for f in self.findings if f.action and (op is None or f.action.op == op)]

    def applied(self, op: str | None = None) -> list[dict]:
        return [r for r in self.report.applied if op is None or r["op"] == op]

    def skipped(self, op: str | None = None) -> list[dict]:
        return [r for r in self.report.skipped if op is None or r["op"] == op]

    def failed(self, op: str | None = None) -> list[dict]:
        return [r for r in self.report.failed if op is None or r["op"] == op]

    def unknown(self, op: str | None = None) -> list[dict]:
        return [r for r in self.report.unknown if op is None or r["op"] == op]


@dataclass
class Loop(Cycle):
    """一轮 `run` 的迭代（`converge.run`）：`findings` / `state` 是最后一次诊断的，`report` 是整轮累计的。"""
    outcome: converge.Outcome | None = None

    @property
    def iterations(self) -> list:
        return [it for it in self.outcome.iterations if not it.final]


class LibraryBuilder:
    """见模块文档。一般通过 conftest 的 `lib` fixture 拿到。"""

    def __init__(self, root: Path, *, project_root: Path, tripwire=None,
                 web: FakeWeb | None = None, probe: FakeProbe | None = None):
        self.root = Path(root).resolve()
        self.media_root = self.root / "Media"
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.project_root = Path(project_root)
        self.tripwire = tripwire
        self.web = web or FakeWeb(tripwire)
        self.probe = probe or FakeProbe()
        self.qbit = FakeQbit(tripwire, default_save_path=self.root / "downloads")
        self.tmdb = FakeTMDB(tripwire)
        self.llm = FakeLLM(tripwire)
        self.abdb = None
        self.docker_log: Path | None = None
        self.ab = FakeAB(tripwire)
        self.logs: list[str] = []
        self.qbit_up = True
        self._n = 0
        self._cycles = 0
        stub, self.docker_log = make_docker_stub(self.root / "bin")
        self.cfg = Config(
            media_root=self.media_root,
            qbit_url="http://qbit.invalid", qbit_user="test", qbit_pass="test",
            ab_url="http://ab.invalid", ab_user="", ab_pass="",
            ab_db=Path(""), ab_container="autobangumi", docker_bin=str(stub),
            tmdb_api_key="", tmdb_lang="zh-CN",
            llm_base="http://llm.invalid", llm_key="", llm_model="test-model-2026-01-01",
            auto_apply=True, trash_retention_days=30,
            max_delete_per_run=50, max_delete_gb_per_run=200.0, dead_torrent_hours=48,
        )
        assert self.cfg.state_dir == self.project_root / "state", (
            "PROJECT_ROOT 没被打到临时目录——审计/隔离区会写进仓库的 state/")

    # ================================================================ 布置现场
    def configure(self, **overrides) -> Config:
        """改 Config 字段（如 `max_delete_per_run=1`、`dead_torrent_hours=1`）。"""
        self.cfg = dataclasses.replace(self.cfg, **overrides)
        return self.cfg

    def show(self, name: str) -> ShowBuilder:
        return ShowBuilder(self, name)

    def movie(self, dir_name: str, filename: str, size: int = 4_000_000_000, *,
              probe: ProbeSpec | None = None) -> Path:
        """电影目录：没有 `Season N`、视频不超过 2 个（scan._looks_like_movie）。"""
        return self._write(self.media_root / dir_name / filename, size, probe=probe)

    def bangumi(self, *, id: int, official_title: str, title_raw: str, **kw) -> dict:
        """AutoBangumi 订阅行。第一次调用时建临时 AB 库并挂到 cfg.ab_db。"""
        if self.abdb is None:
            self.abdb, self.docker_log = make_ab_db(self.root / "ab")
            self.ab.abdb = self.abdb
            self.cfg = dataclasses.replace(self.cfg, ab_db=Path(self.abdb.db_path),
                                           docker_bin=self.abdb.docker_bin)
        return insert_bangumi(self.abdb, id=id, official_title=official_title,
                              title_raw=title_raw, **kw)

    def ab_rows(self, table: str, rows: list[dict]) -> None:
        """往 AB 的 rssitem / torrent 表塞行（需先 `bangumi()` 建库）。"""
        assert self.abdb is not None, "先调用 lib.bangumi(...) 建 AB 库"
        insert_rows(self.abdb, table, rows)

    def mikan(self, mikan_id: str, items: list[MikanItem], *,
              search: Iterable[str] = ()) -> None:
        """番组页 feed + 若干搜索词都指向它。`grab._resolve_mikan_id` 会拿
        canonical_title / official_title / 每个别名去搜，漏配哪个都会被 tripwire 抓到。"""
        self.web.mikan_feed(mikan_id, items)
        for kw in search:
            self.web.mikan_search(kw, [mikan_id])

    def fs_move(self, src_rel: str, dst_rel: str) -> Path:
        """绕开 qBittorrent 的文件系统 mv（造 stale-torrent-path 这类现场）。"""
        src, dst = self.media_root / src_rel, self.media_root / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst

    def docker_fail(self, verb: str) -> None:
        """之后 `docker <verb>`（stop / start）以 1 退出。AB 数据库的写是"停容器 → 改库 → 起容器"，
        `docker_fail("start")` 就是库已提交、容器没起来。"""
        assert self.docker_log is not None
        (self.docker_log.parent / f"fail-{verb}").write_text("", encoding="utf-8")

    def qbit_down(self) -> None:
        """之后新建的 Context 里 `qbit=None`——即 build_context 登录失败的形态。"""
        self.qbit_up = False

    # ================================================================ 运行
    def _log(self, msg, *a, **k) -> None:
        self.logs.append(str(msg))

    def context(self, **overrides) -> Context:
        """新建一个 Context。`overrides` 可替换任何 capability（如 `tmdb=None`）。"""
        caps = dict(qbit=self.qbit if self.qbit_up else None, ab=self.ab,
                    abdb=self.abdb, tmdb=self.tmdb, anilist=None, llm=self.llm,
                    logger=self._log)
        caps.update(overrides)
        return Context(self.cfg, **caps)

    def scan(self, *, resolve_tmdb: bool = True, ctx: Context | None = None) -> LibraryState:
        return build_state(ctx or self.context(), resolve_tmdb=resolve_tmdb)

    def registry(self, detectors: Iterable | None = None) -> Registry:
        """默认 = 生产的内置规则全集（不含演进规则）；也可只给几个检测器实例。"""
        if detectors is None:
            return register_builtins(Registry())
        reg = Registry()
        for d in detectors:
            reg.register(d() if isinstance(d, type) else d)
        return reg

    def diagnose(self, state: LibraryState | None = None, *, ctx: Context | None = None,
                 detectors: Iterable | None = None) -> list[Finding]:
        ctx = ctx or self.context()
        state = state if state is not None else self.scan(ctx=ctx)
        return self.registry(detectors).run_all(ctx, state)

    def next_run_id(self) -> str:
        self._cycles += 1
        return f"t{self._cycles:03d}"

    def apply(self, findings: list[Finding], *, dry_run: bool = False,
              run_id: str | None = None, ctx: Context | None = None) -> ExecReport:
        return Executor(ctx or self.context(), dry_run=dry_run,
                        run_id=run_id or self.next_run_id()).apply(findings)

    def cycle(self, *, dry_run: bool = False, run_id: str | None = None,
              resolve_tmdb: bool = True, detectors: Iterable | None = None,
              select: Callable[[Finding], bool] | None = None) -> Cycle:
        """一轮：新进程语义（清缓存）→ 新 Context → 扫描 → 诊断 → 执行。

        `select` 过滤要执行的 finding（相当于 `apply --kind`），诊断结果仍完整保留。
        """
        reset_process_caches()
        ctx = self.context()
        state = build_state(ctx, resolve_tmdb=resolve_tmdb)
        findings = self.registry(detectors).run_all(ctx, state)
        todo = [f for f in findings if select is None or select(f)]
        rid = run_id or self.next_run_id()
        report = Executor(ctx, dry_run=dry_run, run_id=rid).apply(todo)
        return Cycle(rid, ctx, state, findings, report)

    def loop(self, *, max_iterations: int = 3, dry_run: bool = False, run_id: str | None = None,
             resolve_tmdb: bool = True, detectors: Iterable | None = None,
             select: Callable[[Finding], bool] | None = None) -> Loop:
        """一轮 `run` 的迭代：新进程语义（清缓存）→ **一个** Context、**一个**执行器 → 扫描 → 诊断 → 执行，
        重复到不动点（`converge.run`）。`cycle()` 是只跑一次的旧形态。"""
        reset_process_caches()
        ctx = self.context()
        reg = self.registry(detectors)
        rid = run_id or self.next_run_id()
        ex = Executor(ctx, dry_run=dry_run, run_id=rid)
        out = converge.run(ctx, reg, ex, max_iterations=max_iterations, select=select,
                           scan=lambda n: build_state(ctx, resolve_tmdb=resolve_tmdb))
        return Loop(rid, ctx, out.state, out.findings, ex.report, outcome=out)

    def grab_loop(self, *, max_iterations: int = 3, dry_run: bool = False, run_id: str | None = None) -> Loop:
        """一轮抓取模式（`media-agent grab`，`grabmode.run`）：同 `loop()`，检测器与挑法是抓取模式的。"""
        from media_agent import grabmode

        reset_process_caches()
        ctx = self.context()
        rid = run_id or self.next_run_id()
        ex = Executor(ctx, dry_run=dry_run, run_id=rid)
        out = grabmode.run(ctx, ex, max_iterations=max_iterations,
                           scan=lambda n: build_state(ctx, resolve_tmdb=True))
        return Loop(rid, ctx, out.state, out.findings, ex.report, outcome=out)

    def converge(self, *, max_rounds: int = 5, **kw) -> list[Cycle]:
        """反复 `cycle()` 直到某一轮什么都没执行。到上限仍在变就判失败——
        每轮都"成功"却原地打转，正是本库最难发现的一类故障（入间同学空转三天）。"""
        out = []
        for _ in range(max_rounds):
            c = self.cycle(**kw)
            out.append(c)
            if not c.report.applied:
                return out
        raise AssertionError(
            f"{max_rounds} 轮仍未收敛；最后一轮执行了："
            + "; ".join(f"{r['op']} {r.get('summary', '')[:60]}" for r in out[-1].report.applied))

    def rollback(self, run_id: str, *, dry_run: bool = False) -> dict:
        reset_process_caches()
        return Executor(self.context(), dry_run=dry_run, run_id=f"rb-{run_id}").rollback(run_id)

    # ================================================================ 观察
    def audit(self, run_id: str | None = None) -> list[dict]:
        p = self.cfg.audit_log
        if not p.exists():
            return []
        recs = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
        return [r for r in recs if run_id is None or r.get("run_id") == run_id]

    def trash_files(self) -> list[Path]:
        return sorted(p for p in self.cfg.trash_dir.rglob("*") if p.is_file())

    def path(self, rel: str) -> Path:
        return self.media_root / rel

    def rel(self, p: str | Path) -> str:
        return str(Path(p).relative_to(self.media_root))

    def disk(self, *, sidecars: bool = False) -> dict[str, int]:
        """媒体根下所有文件 → 大小（默认不含 sidecar，它每次写都带时间戳）。"""
        out = {}
        for p in sorted(self.media_root.rglob("*")):
            if p.is_file() and (sidecars or p.name != sc_mod.SIDECAR_NAME):
                out[self.rel(p)] = p.stat().st_size
        return out

    def sidecar(self, show: str) -> sc_mod.Sidecar:
        return sc_mod.load(self.media_root / show)

    def snapshot(self) -> dict:
        """磁盘 + qBittorrent 的规范化快照，给"回退后与原来一致"这类性质断言用。"""
        return {"disk": self.disk(), "qbit": self.qbit.snapshot()}

    def ident(self, p: Path) -> str | None:
        """文件头里的身份标记——判断"留下的是不是那一份"比比较路径可靠。"""
        return read_ident(p)

    # ================================================================ 内部
    def _next_hash(self, seed: str) -> str:
        self._n += 1
        return hashlib.sha1(f"{seed}#{self._n}".encode("utf-8")).hexdigest()

    def _write(self, path: Path, size: int, *, probe: ProbeSpec | None = None,
               ident: str | None = None, same_content_as: Path | None = None) -> Path:
        if same_content_as is not None:
            ident = read_ident(same_content_as)
            assert ident, f"{same_content_as} 不是 builder 造的文件"
        if ident is None:
            self._n += 1
            ident = f"local:{self._n}"
        write_sparse(path, size, ident)
        if probe is not None:
            self.probe.register(ident, probe)
        return path
