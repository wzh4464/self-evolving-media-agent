"""离线测试的总闸。每个测试自动获得：

1. **隔离**：`PROJECT_ROOT` → 临时目录（审计 / 隔离区 / cache.sqlite3 都落在那），
   不读仓库的 `.env`（真 `.env` 里有凭据，`_load_dotenv` 写进 `os.environ` 后
   monkeypatch 撤不回来），演进器的 rules / notes 目录也打到临时目录。
2. **断网**：`urllib.request.urlopen` → FakeWeb；socket 直连一律拦下。
3. **不起子进程**：`subprocess.run` 只放行测试自己 tmp 目录里的替身脚本
   （docker stub）；`@pytest.mark.ffmpeg` 的测试另外放行 ffprobe / ffmpeg。
4. **假 ffprobe**：`media_agent.probe._run` → FakeProbe（开发机上真 ffprobe 是存在的）。
5. **Tripwire**：Executor 的 failed 审计、Registry 吞掉的检测器异常、没路由的 URL、
   假对象没建模的方法……测试结束时未声明的一律判失败。

`@pytest.mark.live` 的测试跳过以上全部——它们本来就是要碰真库的（只读），
而且默认不跑：`addopts` 里 `-m "not live"`，另外还要 `MEDIA_AGENT_LIVE=1`。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from harness import FakeProbe, FakeWeb, LibraryBuilder, Tripwire
from harness.library import reset_process_caches
from harness.torrentfile import write_sparse

from media_agent import config as config_mod
from media_agent import evolution as evolution_mod
from media_agent import kernel as kernel_mod
from media_agent import probe as probe_mod
from media_agent.actions import Executor
from media_agent.kernel import MediaFile

# load_config() 会读的全部环境变量。清掉它们，任何意外的 load_config()
# 都只会拿到默认值，而不是开发机 shell 里残留的真实地址。
_CONFIG_ENV = ("MEDIA_ROOT", "QBIT_URL", "QBIT_USER", "QBIT_PASS", "AB_URL", "AB_USER",
               "AB_PASS", "AB_DB", "AB_CONTAINER", "DOCKER_BIN", "TMDB_API_KEY",
               "TMDB_LANG", "LLM_BASE", "LLM_KEY", "LLM_MODEL", "AUTO_APPLY",
               "TRASH_RETENTION_DAYS", "MAX_DELETE_PER_RUN", "MAX_DELETE_GB_PER_RUN",
               "DEAD_TORRENT_HOURS", "EVOLVE_MODE", "QBIT_ALLOW_EMPTY",
               "QUARANTINE_MIN_AGE_DAYS", "MIN_FREE_GB")


def _is_live(request) -> bool:
    return request.node.get_closest_marker("live") is not None


# ------------------------------------------------------------------ 收集阶段
def pytest_collection_modifyitems(config, items):
    live_ok = os.environ.get("MEDIA_AGENT_LIVE") == "1"
    has_ff = bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))
    skip_live = pytest.mark.skip(
        reason="live：只读核对生产库，需在 zihan_air 上设 MEDIA_AGENT_LIVE=1 并 -m live")
    skip_ff = pytest.mark.skip(reason="需要真实的 ffprobe / ffmpeg")
    for item in items:
        if "live" in item.keywords and not live_ok:
            item.add_marker(skip_live)
        if "ffmpeg" in item.keywords and not has_ff:
            item.add_marker(skip_ff)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"_rep_{rep.when}", rep)     # 给 tripwire 判断"测试本体是否已失败"
    return rep


# ------------------------------------------------------------------ 基础 fixture
@pytest.fixture
def tripwire(request):
    tw = Tripwire()
    for m in request.node.iter_markers("allow"):
        for kind in m.args:
            tw.allow(kind, m.kwargs.get("match"))
    yield tw
    if _is_live(request):
        return
    if tw.unexpected():
        call = getattr(request.node, "_rep_call", None)
        if call is not None and call.failed:
            # 本体已经失败了，别再叠一个 teardown error；把事件附在报告里即可
            request.node.add_report_section("teardown", "tripwire", tw.report())
        else:
            pytest.fail(tw.report(), pytrace=False)


@pytest.fixture
def project_root(tmp_path) -> Path:
    p = (tmp_path / "project").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def web(tripwire) -> FakeWeb:
    return FakeWeb(tripwire)


@pytest.fixture
def fake_probe() -> FakeProbe:
    return FakeProbe()


@pytest.fixture(autouse=True)
def _offline(request, monkeypatch, tmp_path, project_root, tripwire, web, fake_probe):
    if _is_live(request):
        yield
        return

    # 1) 隔离：state/、.env、演进产物
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config_mod, "_load_dotenv", lambda path: None)
    for k in _CONFIG_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(evolution_mod, "RULES_DIR", project_root / ".agents" / "rules")
    monkeypatch.setattr(evolution_mod, "NOTES_ROOT", project_root / ".agents" / "notes")

    # 2) 断网
    monkeypatch.setattr(urllib.request, "urlopen", web.urlopen)

    def _blocked(what):
        def guard(*a, **k):
            tripwire.record("network", f"{what}{a[1:2] if what == 'connect' else a[:1]}")
            raise OSError(f"离线测试禁止联网：{what}")
        return guard

    real_connect = socket.socket.connect

    def connect(self, address, *a, **k):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            return _blocked("connect")(self, address)
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", _blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _blocked("getaddrinfo"))

    # 3) 子进程守卫
    real_run = subprocess.run
    allow_ff = request.node.get_closest_marker("ffmpeg") is not None
    tmp_root = str(tmp_path.resolve())

    def guarded_run(args, *a, **k):
        exe = str(args[0]) if isinstance(args, (list, tuple)) else str(args).split()[0]
        ok = exe.startswith(tmp_root + os.sep) or (
            allow_ff and Path(exe).name in ("ffprobe", "ffmpeg"))
        if not ok:
            tripwire.record("subprocess", " ".join(map(str, args))[:200])
            raise FileNotFoundError(f"离线测试禁止起子进程：{exe}")
        return real_run(args, *a, **k)

    monkeypatch.setattr(subprocess, "run", guarded_run)

    # 4) 假 ffprobe + 进程级缓存
    reset_process_caches()
    if not allow_ff:
        monkeypatch.setattr(probe_mod, "_run", fake_probe.run)

    # 5) 磁盘剩余空间：固定为充足。容量闸（MIN_FREE_GB）与隔离前的空间检查读 statvfs——结果不能
    #    随开发机 / CI 的磁盘而变；要测空间不足的用例自己再 monkeypatch `disposal.free_bytes`。
    from media_agent import disposal as disposal_mod
    monkeypatch.setattr(disposal_mod, "free_bytes", lambda path: 10**15)

    # 6) tripwire 接线：failed 审计、被吞的检测器异常 / 失败日志
    real_audit = Executor._audit

    def audit(self, status, finding, action, extra=None, undo=None):
        n = len(self.report.audit_problems)
        real_audit(self, status, finding, action, extra, undo)
        if status == "failed":
            tripwire.record("failed_record",
                            f"{action.op} [{finding.rule}] {(extra or {}).get('error', '')}")
        for p in self.report.audit_problems[n:]:
            tripwire.record("audit_fallback", p)

    monkeypatch.setattr(Executor, "_audit", audit)

    real_init = kernel_mod.Context.__init__

    def ctx_init(self, *a, **k):
        real_init(self, *a, **k)
        inner = self.log

        def log(msg, *la, **lk):
            tripwire.on_log(str(msg))
            return inner(msg, *la, **lk)

        self.log = log

    monkeypatch.setattr(kernel_mod.Context, "__init__", ctx_init)

    yield
    reset_process_caches()


# ------------------------------------------------------------------ 构造器
@pytest.fixture
def lib(tmp_path, project_root, tripwire, web, fake_probe) -> LibraryBuilder:
    """一个空媒体库 + 全套替身。见 tests/harness/library.py。"""
    return LibraryBuilder(tmp_path / "lib", project_root=project_root,
                          tripwire=tripwire, web=web, probe=fake_probe)


@pytest.fixture
def make_file(tmp_path, fake_probe):
    """造一个真实存在的文件并返回对应的 `MediaFile`（不经 scan，给单元测试用）。

        f = make_file("尼古喵喵 S01E10.mkv", torrent_name=XIE,
                      probe=video("hevc", subs=["chi 简体中文"]))

    `probe=None` 时 FakeProbe 对它返回"探不到"（等同于没装 ffprobe）。
    `exists=False` 则只给路径不落盘——与旧脚本里 `/tmp/does-not-exist` 的写法等价。
    """
    root = (tmp_path / "files" / "Media").resolve()
    counter = iter(range(1, 10_000))

    def _make(filename: str, torrent_name: str = "", *, size: int = 646_053_213,
              show: str = "尼古喵喵", season_dir: str = "Season 1", category: str = "",
              tags: str = "", torrent_hash: str = "", progress: float = 1.0,
              probe=None, exists: bool = True) -> MediaFile:
        d = root / show / season_dir if season_dir else root / show
        path = d / filename
        if exists:
            ident = f"make_file:{next(counter)}"
            write_sparse(path, size, ident)
            if probe is not None:
                fake_probe.register(ident, probe)
        return MediaFile(path=path, size=size, show_dir=show, season_dir=season_dir,
                         filename=filename, torrent_hash=torrent_hash,
                         torrent_name=torrent_name or filename,
                         torrent_state="stalledUP" if progress >= 1 else "downloading",
                         torrent_progress=progress, torrent_tags=tags,
                         torrent_category=category)

    return _make


# ------------------------------------------------------------------ 文件系统语义
@pytest.fixture(params=["native", "case-sensitive"])
def fs(request, monkeypatch):
    """路径占用（`media_agent.claims`）盘上那一侧在两种文件系统语义下都要成立。

    本机（macOS）的临时目录是大小写 / 规范化都不敏感的 APFS，直接 lstat 就认得出
    `s01e08.MKV`；CI 的 Linux 区分大小写，只能靠列目录折叠比较。`case-sensitive`
    把 lstat 换成"名字必须逐字节出现在父目录列表里"，在本机也把后一条路径跑一遍。
    """
    if request.param == "case-sensitive":
        from media_agent import claims as claims_mod
        real = claims_mod._lstat_key

        def exact(p):
            p = Path(p)
            try:
                if p.name not in os.listdir(p.parent):
                    return None
            except OSError:
                return None
            return real(p)

        monkeypatch.setattr(claims_mod, "_lstat_key", exact)
    return request.param
