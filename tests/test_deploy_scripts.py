"""deploy/deploy.sh 与 deploy/convert-to-git.sh：在临时目录里模拟生产目录跑一遍。

绝不碰真生产：生产目录、上游仓库、LaunchAgents、launchctl、uv 全是临时目录里的替身。
uv 替身只造 `.venv/bin/media-agent`（打印 pyproject 里的版本号），launchctl 替身只记账；
离线测试命令用 `DEPLOY_TEST_CMD` 换成轻量命令——真 uv 0.7.2 + 真 pytest 的完整演练
另在开发机上做过（见 deploy/README.md）。

脚本要在生产机的 macOS bash 3.2 上跑：CI 的 macOS 腿用的就是 /bin/bash 3.2。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from media_agent.runlock import RunLock

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "deploy.sh"
CONVERT = REPO / "deploy" / "convert-to-git.sh"
LABEL = "com.zihan.media-agent"
GRAB = "com.zihan.media-agent-grab"              # 抓取模式的任务（v1.4.0 起才有，与生产的 v0.6.0 起一样）

pytestmark = [
    pytest.mark.skipif(os.environ.get("MEDIA_AGENT_DEPLOYING") == "1",
                       reason="deploy.sh 自己在跑测试集：不递归测部署脚本"),
    pytest.mark.skipif(not (shutil.which("git") and Path("/bin/bash").exists()),
                       reason="需要 git 与 /bin/bash"),
]

GIT_ENV = {
    # 与开发者自己的 git 配置隔离（模板钩子、提交签名、全局 excludesfile……）
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.zihan.media-agent</string>
  <key>ProgramArguments</key>
  <array><string>{prog}</string><string>run</string></array>
</dict>
</plist>
"""

GRAB_PLIST = PLIST.replace("com.zihan.media-agent<", GRAB + "<").replace(">run<", ">grab<")

FAKE_UV = r"""#!/bin/sh
echo "$PWD uv $*" >> "$SANDBOX/uv.log"
# 打断之后的那次 sync（即"自动退回"里的）故意放慢：外层若没等退回做完就先返回，
# 测试就一定看得到停在半路的现场，而不是碰运气赶上退回已经做完。
[ -f "$SANDBOX/interrupted" ] && sleep 1
case "$1" in
  sync) mkdir -p .venv/bin && ln -sf "$TOOLS/media-agent" .venv/bin/media-agent ;;
  lock) ;;
esac
exit 0
"""

# 装进"venv"的入口：按所在工作区的 pyproject.toml 报版本（$0 是 .venv/bin 下的软链）
FAKE_ENTRY = r"""#!/bin/sh
root=$(cd "$(dirname "$0")/../.." && pwd)
echo "media-agent $(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")"
"""

FAKE_LAUNCHCTL = r"""#!/bin/sh
echo "launchctl $*" >> "$SANDBOX/launchctl.log"
case "$1" in
  print) echo "state = ${FAKE_LAUNCHD_STATE:-not running}" ;;
  bootstrap) grep -q BROKEN "$3" && { echo "Bootstrap failed: 5" >&2; exit 5; } ;;
esac
exit 0
"""


def _write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def _release(root: Path, version: str, *, plist_prog: str, extra: dict[str, str]) -> None:
    """一个发布的内容：只要部署脚本关心的那些文件。"""
    _write(root / "pyproject.toml", f'[project]\nname = "media-agent"\nversion = "{version}"\n')
    _write(root / "uv.lock", "version = 1\n")
    _write(root / "media_agent" / "core.py", f"VERSION = {version!r}\n")
    # 像 v0.1.0 那样只有 5 行的旧 .gitignore：挡不住 .env.bak-* / *.bak-* / .DS_Store
    _write(root / ".gitignore", ".env\n.venv/\n__pycache__/\n*.pyc\nstate/\n")
    _write(root / ".agents" / "preferences.json", '{"require": []}\n')
    _write(root / ".agents" / "rules" / "r1.json", '{"id": "r1"}\n')
    _write(root / "AGENTS.md", f"# AGENTS {version}\n")
    _write(root / "README.md", f"readme {version}\n")
    _write(root / "deploy" / f"{LABEL}.plist", PLIST.format(prog=plist_prog))
    for rel, text in extra.items():
        if text is None:                                   # 这个发布里删掉了它
            (root / rel).unlink(missing_ok=True)
            continue
        _write(root / rel, text, 0o755 if rel.endswith(".sh") else None)


@pytest.fixture(scope="module")
def upstream(tmp_path_factory) -> Path:
    """上游仓库：v1.0.0（基线）→ v1.1.0（多一条规则、一篇笔记，plist 变了）→
    v1.2.0（代码变了）→ v1.3.0（plist 装不上）→ v1.4.0（多了抓取的任务）→ v1.5.0（抓取的任务变了）
    → v1.6.0（抓取的任务装不上）→ v1.7.0（又没有抓取的任务了）。v1.4.0 起主任务与 v1.2.0 相同。

    模块级 fixture 先于每个测试的子进程守卫建立，这里直接用 subprocess。
    """
    root = tmp_path_factory.mktemp("upstream")
    work = root / "work"
    env = {**os.environ, **GIT_ENV, "HOME": str(root)}

    def git(*args):
        subprocess.run(["git", "-C", str(work), *args], check=True, env=env,
                       capture_output=True)

    work.mkdir()
    git("init", "-q", "-b", "main")
    releases = [
        ("1.0.0", "/app/.venv/bin/media-agent", {}),
        # 与生产的 v0.1.0 → v0.2.0 一样：部署脚本是后来才入库的
        ("1.1.0", "/app/.venv/bin/media-agent --v11",
         {".agents/rules/r2.json": '{"id": "r2"}\n',
          ".agents/notes/implemented/process/n1.md": "# note\n",
          "deploy/deploy.sh": DEPLOY.read_text(encoding="utf-8")}),
        ("1.2.0", "/app/.venv/bin/media-agent --v11", {"media_agent/new.py": "X = 1\n"}),
        ("1.3.0", "/app/.venv/bin/media-agent BROKEN", {}),
        ("1.4.0", "/app/.venv/bin/media-agent --v11",
         {f"deploy/{GRAB}.plist": GRAB_PLIST.format(prog="/app/.venv/bin/media-agent")}),
        ("1.5.0", "/app/.venv/bin/media-agent --v11",
         {f"deploy/{GRAB}.plist": GRAB_PLIST.format(prog="/app/.venv/bin/media-agent --v15")}),
        ("1.6.0", "/app/.venv/bin/media-agent --v11",
         {f"deploy/{GRAB}.plist": GRAB_PLIST.format(prog="/app/.venv/bin/media-agent BROKEN")}),
        ("1.7.0", "/app/.venv/bin/media-agent --v11", {f"deploy/{GRAB}.plist": None}),
    ]
    for version, prog, extra in releases:
        _release(work, version, plist_prog=prog, extra=extra)
        git("add", "-A")
        git("commit", "-q", "-m", f"release {version}")
        git("tag", "-a", "-m", f"v{version}", f"v{version}")
    bare = root / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True,
                   env=env, capture_output=True)
    return bare


def _like_a_terminal() -> None:
    """部署脚本是人在终端里跑的：INT / QUIT 是默认处置。pytest 自己可能是以忽略 SIGINT
    的状态起来的（`uv run pytest … &`，非交互 shell 的后台作业），不复位就会一路继承下去，
    "Ctrl-C 自动退回"的用例便随起 pytest 的方式时红时绿（2026-09-26，[lockf-INT]）。"""
    for s in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, signal.SIG_DFL)


@dataclass
class Sandbox:
    root: Path
    app: Path
    agents_dir: Path
    env: dict
    bash: Path

    def run(self, script: Path, *args: str, new_session: bool = False,
            **env) -> subprocess.CompletedProcess:
        e = {**self.env, **{k: str(v) for k, v in env.items()}}
        return subprocess.run([str(self.bash), str(script), *args], env=e,
                              capture_output=True, text=True, cwd=self.root,
                              start_new_session=new_session, preexec_fn=_like_a_terminal)

    def deploy(self, *args: str, **env) -> subprocess.CompletedProcess:
        return self.run(DEPLOY, *args, **env)

    def git(self, *args: str) -> str:
        r = subprocess.run([str(self.bash), "-c", 'git -C "$0" "$@"', str(self.app), *args],
                           env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def tag_sha(self, tag: str) -> str:
        return self.git("rev-parse", f"{tag}^{{commit}}")

    def history(self) -> list[list[str]]:
        p = self.app / "state" / "deploy.history"
        if not p.exists():
            return []
        return [line.split("\t") for line in p.read_text(encoding="utf-8").splitlines()]

    def launchctl_calls(self) -> list[str]:
        p = self.root / "launchctl.log"
        return p.read_text().splitlines() if p.exists() else []

    def installed_plist(self, label: str = LABEL) -> str:
        return (self.agents_dir / f"{label}.plist").read_text()

    def installed(self, label: str) -> bool:
        return (self.agents_dir / f"{label}.plist").exists()

    def reloads(self, label: str) -> list[str]:
        """launchctl 对这个任务的 bootout / bootstrap（按调用顺序）。"""
        uid = os.getuid()
        out = []
        for c in self.launchctl_calls():
            if c == f"launchctl bootout gui/{uid}/{label}":
                out.append("bootout")
            elif c.startswith(f"launchctl bootstrap gui/{uid} ") and c.endswith(f"/{label}.plist"):
                out.append("bootstrap")
        return out

    def fingerprint(self) -> str:
        """工作区（不含 state/、.venv、.git）的内容指纹。"""
        h = hashlib.sha256()
        for p in sorted(self.app.rglob("*")):
            rel = p.relative_to(self.app)
            if rel.parts[0] in ("state", ".venv", ".git") or not p.is_file():
                continue
            h.update(str(rel).encode() + b"\0" + p.read_bytes() + b"\0")
        return h.hexdigest()


@pytest.fixture(scope="module")
def tools(tmp_path_factory) -> Path:
    """替身脚本整个模块只写一次：macOS 第一次执行一个新写的脚本要被系统检查
    约 0.2 秒，每个测试各写一套会让这个文件慢上好几倍。"""
    d = tmp_path_factory.mktemp("tools")
    _write(d / "bash", '#!/bin/sh\nexec /bin/bash "$@"\n', 0o755)
    _write(d / "uv", FAKE_UV, 0o755)
    _write(d / "launchctl", FAKE_LAUNCHCTL, 0o755)
    _write(d / "media-agent", FAKE_ENTRY, 0o755)
    subprocess.run([str(d / "bash"), "-c", "true"], check=True)       # 预热
    return d


def _tools(tmp_path: Path, tools: Path) -> tuple[Path, Path, Path]:
    # 子进程守卫只放行本测试 tmp 目录里的可执行文件：放一个指向共享脚本的软链
    bash = tmp_path / "bin" / "bash"
    bash.parent.mkdir(parents=True, exist_ok=True)
    bash.symlink_to(tools / "bash")
    return bash, tools / "uv", tools / "launchctl"


@pytest.fixture
def sandbox(tmp_path, upstream, tools) -> Sandbox:
    """转换之后的生产目录：v1.0.0 的 git 工作区 + .env + state/ + 已装的 plist。"""
    bash, uv, launchctl = _tools(tmp_path, tools)
    agents = tmp_path / "LaunchAgents"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {**os.environ, **GIT_ENV, "HOME": str(tmp_path), "SANDBOX": str(tmp_path),
           "TOOLS": str(tools), "TMPDIR": str(tmpdir), "UV_BIN": str(uv),
           "LAUNCHCTL": str(launchctl), "LAUNCH_AGENTS_DIR": str(agents),
           "DEPLOY_LOCK_WAIT": "1", "LAUNCHD_BOOTSTRAP_TRIES": "1",
           "DEPLOY_TEST_CMD": "test -x .venv/bin/media-agent"}
    for k in ("MEDIA_AGENT_DEPLOYING", "VIRTUAL_ENV", "FAKE_LAUNCHD_STATE"):
        env.pop(k, None)
    app = tmp_path / "home" / "media-agent"
    env["MEDIA_AGENT_HOME"] = str(app)
    sb = Sandbox(tmp_path, app, agents, env, bash)
    r = subprocess.run([str(bash), "-c", 'git clone -q "$0" "$1" && git -C "$1" checkout -q --detach v1.0.0 '
                        '&& cd "$1" && "$2" sync', str(upstream), str(app), str(uv)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _write(app / ".env", "QBIT_PASS=synthetic\n", 0o600)
    _write(app / "state" / "audit.jsonl", '{"run_id": "20260920T170126"}\n')
    _write(agents / f"{LABEL}.plist", (app / "deploy" / f"{LABEL}.plist").read_text())
    # convert-to-git.sh 在转换结束时写的那一行（部署判断"已经是这个版本"要认它）
    _write(app / "state" / "deploy.history",
           f"2026-09-26T00:00:00+0800\tv1.0.0\t{sb.tag_sha('v1.0.0')}\tpre-git\tconverted\t"
           "snapshot=synthetic\n")
    return sb


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


# ================================================================== deploy.sh
@pytest.mark.parametrize("ref", ["main", "HEAD~1", "1.1.0", "v1.1", "v9.9.9"])
def test_refuses_anything_but_an_existing_release_tag(sandbox, ref):
    before = sandbox.head()
    r = sandbox.deploy(ref)
    assert r.returncode != 0
    assert "只部署 tag" in _out(r) or "没有这个 tag" in _out(r)
    assert sandbox.head() == before


def test_refuses_a_commit_sha(sandbox):
    r = sandbox.deploy(sandbox.tag_sha("v1.1.0"))
    assert r.returncode != 0 and "只部署 tag" in _out(r)


def test_deploys_a_tag_in_place(sandbox):
    (sandbox.app / ".agents" / "rules" / "r2.json").write_text('{"id": "r2"}\n')  # 与 tag 相同

    r = sandbox.deploy("v1.1.0")

    assert r.returncode == 0, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.1.0")
    assert sandbox.git("status", "--porcelain") == ""          # 同内容的未入库文件由 tag 接管
    assert "media-agent 1.1.0" in r.stdout
    when, tag, sha, prev, result, detail = sandbox.history()[-1]
    assert (tag, sha, prev, result) == ("v1.1.0", sandbox.tag_sha("v1.1.0"),
                                        sandbox.tag_sha("v1.0.0"), "ok")
    # plist 变了 → bootout + bootstrap，装上的是 tag 里那份
    assert "--v11" in sandbox.installed_plist()
    calls = sandbox.launchctl_calls()
    assert any(c.startswith(f"launchctl bootout gui/{os.getuid()}/{LABEL}") for c in calls)
    assert any(c.startswith(f"launchctl bootstrap gui/{os.getuid()} ") for c in calls)
    # 小体量状态留了备份；运行锁文件保留、自述已清空；暂存 worktree 已清理
    [bk] = (sandbox.app / "state" / "backups").iterdir()
    assert (bk / "audit.jsonl").exists() and (bk / f"{LABEL}.plist").exists()
    assert (sandbox.app / "state" / "run.lock").read_text() == ""
    assert len(sandbox.git("worktree", "list").splitlines()) == 1
    assert not list((sandbox.root / "tmp").glob("media-agent-stage.*"))


def test_backup_includes_the_audit_fallback_file(sandbox):
    """审计写不进主文件时的转写（`state/audit.fallback.jsonl`）也是审计：回退、runs 一起读它。"""
    _write(sandbox.app / "state" / "audit.fallback.jsonl", '{"run_id": "20260926T031500.000-1"}\n')

    assert sandbox.deploy("v1.1.0").returncode == 0

    [bk] = (sandbox.app / "state" / "backups").iterdir()
    assert (bk / "audit.fallback.jsonl").read_text() == '{"run_id": "20260926T031500.000-1"}\n'


def test_unchanged_plist_is_left_alone(sandbox):
    assert sandbox.deploy("v1.1.0").returncode == 0
    (sandbox.root / "launchctl.log").unlink()

    r = sandbox.deploy("v1.2.0")

    assert r.returncode == 0, _out(r)
    assert "launchd 配置未变" in r.stdout
    assert not [c for c in sandbox.launchctl_calls() if "bootstrap" in c or "bootout" in c]


def test_already_deployed_is_a_no_op(sandbox):
    before = sandbox.history()
    r = sandbox.deploy("v1.0.0")
    assert r.returncode == 0 and "已经是 v1.0.0" in r.stdout
    assert sandbox.history() == before


def test_already_deployed_after_a_successful_deploy_is_a_no_op(sandbox):
    assert sandbox.deploy("v1.1.0").returncode == 0
    n = len(sandbox.history())

    r = sandbox.deploy("v1.1.0")

    assert r.returncode == 0 and "已经是 v1.1.0" in r.stdout
    assert len(sandbox.history()) == n


# ------------------------------------------------------------------ 中途被打断
# 审查复现（2026-09-26）：switch 阶段只装了 `trap … EXIT`。ssh 断线（SIGHUP）或 Ctrl-C
# 打在原地 `uv sync` / 离线测试上，切换直接被杀：HEAD 已是新 tag、venv 还是旧锁文件装的、
# plist 没换、deploy.history 一行没有、临时文件泄漏。再跑同一个 tag，`SHA == PREV`
# 的短路报「已经是 v1.1.0，无需部署」并返回 0——venv 与 plist 永远不会被补上。
_IN_PLACE = 'case "$PWD" in *media-agent-stage*) ;; *) {} ;; esac; test -x .venv/bin/media-agent'

# 持锁包装有三种实现（macOS 的 lockf / Linux 的 flock / 都没有时的 Python），信号行为各不相同。
# 只测平台默认的那一种会漏：v0.2.0 在本机 macOS 全绿，CI 的 Ubuntu 上 flock 分支一收到
# SIGINT 就先死，退回成了后台孤儿。每个平台把它能跑的实现都跑一遍。
LOCK_IMPLS = [impl for impl, ok in (("lockf", os.access("/usr/bin/lockf", os.X_OK)),
                                     ("flock", shutil.which("flock") is not None),
                                     ("python", True)) if ok]


@pytest.fixture(params=LOCK_IMPLS)
def lock_impl(request) -> str:
    return request.param


def test_hangup_during_the_in_place_switch_does_not_kill_it(sandbox, lock_impl):
    """ssh 断线：整个进程组收到 SIGHUP。部署要么完整做完，要么完整退回，不能停在半路。"""
    r = sandbox.deploy("v1.1.0", new_session=True, DEPLOY_LOCK_IMPL=lock_impl,
                       DEPLOY_TEST_CMD=_IN_PLACE.format("kill -HUP 0"))

    assert r.returncode == 0, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.1.0")
    assert sandbox.history()[-1][4] == "ok"
    assert "--v11" in sandbox.installed_plist()
    log = (sandbox.app / "state" / "deploy.log").read_text(encoding="utf-8")
    assert "已部署 v1.1.0" in log                             # 终端没了也看得到结果
    assert not list((sandbox.root / "tmp").glob("ma-*"))       # 临时文件照常清理


@pytest.mark.parametrize("sig", ["INT", "TERM"])
def test_interrupt_during_the_in_place_switch_reverts(sandbox, lock_impl, sig):
    """Ctrl-C / SIGTERM 打在原地测试上：自动退回部署前的版本，并记下来。"""
    before = sandbox.fingerprint()

    r = sandbox.deploy("v1.1.0", new_session=True, DEPLOY_LOCK_IMPL=lock_impl,
                       DEPLOY_TEST_CMD=_IN_PLACE.format(
                           f'touch "$SANDBOX/interrupted"; kill -{sig} 0'))

    assert r.returncode != 0
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.fingerprint() == before
    assert sandbox.history()[-1][4] == "reverted" and "中断" in sandbox.history()[-1][5]
    assert "--v11" not in sandbox.installed_plist()


@pytest.fixture
def pytest_ignores_sigint():
    """模拟 `uv run pytest … &`：没有作业控制的 shell 把后台作业的 SIGINT 置为忽略，
    这个处置经 exec / fork 一路继承；bash 对"进来时就被忽略的信号"装不上 trap。"""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)


def test_interrupt_reverts_even_when_pytest_was_started_with_sigint_ignored(
        sandbox, lock_impl, pytest_ignores_sigint):
    """2026-09-26 的"并发 pytest 时 [lockf-INT] 偶发变红"：与并发无关，是后台起的 pytest
    （`… &`）继承了 SIGINT=忽略。lockf 不动信号处置，切换阶段的 bash 装不上 INT 的 trap、
    `kill -INT 0` 打不动任何人，部署照常做完、返回 0；Python 持锁实现的 preexec_fn 恰好把
    INT 复位，所以只有 lockf 一格红。沙盒要像终端里的人那样起部署脚本。"""
    before = sandbox.fingerprint()

    r = sandbox.deploy("v1.1.0", new_session=True, DEPLOY_LOCK_IMPL=lock_impl,
                       DEPLOY_TEST_CMD=_IN_PLACE.format('touch "$SANDBOX/interrupted"; kill -INT 0'))

    assert r.returncode != 0, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.fingerprint() == before
    assert sandbox.history()[-1][4] == "reverted"


def test_rerun_after_an_interrupted_switch_redoes_the_switch(sandbox):
    """旧脚本被杀后的现场：HEAD 已是新 tag，venv / plist / history 都停在旧版本。
    重跑同一个 tag 不能只看 HEAD 就说"已经是"。"""
    sandbox.git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "v1.1.0")
    (sandbox.root / "uv.log").write_text("")

    r = sandbox.deploy("v1.1.0")

    assert r.returncode == 0, _out(r)
    assert "已经是" not in r.stdout
    assert f"{sandbox.app} uv sync --frozen" in (sandbox.root / "uv.log").read_text()
    assert "--v11" in sandbox.installed_plist()
    assert sandbox.history()[-1][1:5] == ["v1.1.0", sandbox.tag_sha("v1.1.0"),
                                         sandbox.tag_sha("v1.1.0"), "ok"]


# ------------------------------------------------------------------ 回滚提示
def test_rollback_hint_names_the_in_tree_script_when_the_tag_has_one(sandbox):
    r = sandbox.deploy("v1.1.0")
    assert r.returncode == 0, _out(r)
    assert "回滚：deploy/deploy.sh v1.0.0" in r.stdout
    assert (sandbox.app / "deploy" / "deploy.sh").exists()


def test_rollback_hint_works_when_the_deployed_tag_has_no_deploy_script(sandbox):
    """回滚到 v0.1.0 这类还没有 deploy.sh 的 tag 之后，"回滚：deploy/deploy.sh v0.2.0"
    指向一个不存在的文件——恢复的那一刻照着提示做却是 No such file or directory。"""
    assert sandbox.deploy("v1.1.0").returncode == 0
    r = sandbox.deploy("v1.0.0")
    assert r.returncode == 0, _out(r)
    assert not (sandbox.app / "deploy" / "deploy.sh").exists()
    assert "回滚：deploy/deploy.sh" not in r.stdout
    [cmd] = [ln.strip() for ln in r.stdout.splitlines()
             if "show v1.1.0:deploy/deploy.sh" in ln]

    u = subprocess.run([str(sandbox.bash), "-c", cmd], env=sandbox.env,
                       capture_output=True, text=True)

    assert u.returncode == 0, u.stdout + u.stderr
    assert sandbox.head() == sandbox.tag_sha("v1.1.0")


def test_check_mode_stages_but_does_not_switch(sandbox):
    r = sandbox.deploy("v1.1.0", "--check")
    assert r.returncode == 0, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.history()[-1][4] == "checked"
    uv_log = (sandbox.root / "uv.log").read_text()
    assert "media-agent-stage" in uv_log and "sync --frozen" in uv_log and "lock --check" in uv_log


# ------------------------------------------------------------------ 漂移闸门
def test_edited_tracked_file_blocks_the_deploy_and_is_shown(sandbox):
    prefs = sandbox.app / ".agents" / "preferences.json"
    prefs.write_text('{"require": [], "edited_on_prod": true}\n')
    before = sandbox.fingerprint()

    r = sandbox.deploy("v1.1.0", "--harvest")

    assert r.returncode == 1
    assert "拒绝部署" in r.stdout and '+{"require": [], "edited_on_prod": true}' in r.stdout
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.fingerprint() == before                  # 生产上的改动原样保留，没被覆盖
    assert sandbox.history()[-1][4] == "drift"
    [tgz] = (sandbox.app / "state" / "harvest").glob("*-drift.tgz")
    with tarfile.open(tgz) as t:
        assert t.getnames() == [".agents/preferences.json"]


def test_untracked_files_not_in_the_tag_block_and_secrets_never_get_harvested(sandbox):
    app = sandbox.app
    _write(app / ".agents" / "notes" / "implemented" / "process" / "prod-only.md", "# 演进器写的\n")
    _write(app / ".agents" / "rules" / "r1.json.bak-20260905", "old\n")
    _write(app / ".env.bak-20260917T205051", "QBIT_PASS=real-secret\n", 0o600)
    _write(app / ".DS_Store", "")
    _write(app / ".agents" / "rules" / "r2.json", '{"id": "r2", "changed": 1}\n')  # tag 里内容不同

    r = sandbox.deploy("v1.1.0", "--harvest")

    assert r.returncode == 1
    out = r.stdout
    assert "prod-only.md\tv1.1.0 里没有" in out
    assert "r2.json\tv1.1.0 里有同名文件但内容不同" in out
    assert ".env.bak" not in out and ".DS_Store" not in out and "r1.json.bak" not in out
    [tgz] = (app / "state" / "harvest").glob("*-drift.tgz")
    with tarfile.open(tgz) as t:
        assert sorted(t.getnames()) == [".agents/notes/implemented/process/prod-only.md",
                                        ".agents/rules/r2.json"]
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")


# ------------------------------------------------------------------ 失败路径
def test_stage_failure_leaves_production_untouched(sandbox):
    before = sandbox.fingerprint()
    r = sandbox.deploy("v1.1.0", DEPLOY_TEST_CMD="echo boom; exit 1")
    assert r.returncode == 1 and "暂存验证失败" in _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.fingerprint() == before
    assert sandbox.history()[-1][4:] == ["stage-failed", "离线测试"]
    assert len(sandbox.git("worktree", "list").splitlines()) == 1


def test_in_place_failure_reverts_to_the_previous_tag(sandbox):
    r2 = sandbox.app / ".agents" / "rules" / "r2.json"
    r2.write_text('{"id": "r2"}\n')                          # 未入库、与 v1.1.0 相同 → 会被挪开
    before = sandbox.fingerprint()
    only_in_place = 'case "$PWD" in *media-agent-stage*) exit 0;; *) exit 1;; esac'

    r = sandbox.deploy("v1.1.0", DEPLOY_TEST_CMD=only_in_place)

    assert r.returncode == 1, _out(r)
    assert "已退回" in _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.fingerprint() == before                  # 包括被挪开又放回来的 r2.json
    assert r2.read_text() == '{"id": "r2"}\n'
    assert "--v11" not in sandbox.installed_plist()         # plist 在测试之后才装，没动过
    assert sandbox.history()[-1][4:] == ["reverted", "原地离线测试"]


def test_plist_that_fails_to_load_reverts_code_and_launchd(sandbox):
    old_plist = sandbox.installed_plist()

    r = sandbox.deploy("v1.3.0")

    assert r.returncode == 1, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.installed_plist() == old_plist           # 旧 plist 装回去并重新加载了
    assert sandbox.launchctl_calls()[-1].startswith(f"launchctl bootstrap gui/{os.getuid()} ")
    assert sandbox.history()[-1][4:] == ["reverted", "安装 launchd 配置"]


# ------------------------------------------------------------------ 两个 launchd 任务（run 与 grab）
# 第 5 阶段起有两份 plist：每 6 小时的 `run` 与每 30 分钟的 `grab`。部署两份都装、都只在变了时重新加载；
# 装不上就两份一起退回；tag 里没有抓取任务（回滚到更早的版本）就卸掉它——留着的话它每 30 分钟调一个不存在的子命令。
def test_deploy_installs_both_jobs(sandbox):
    r = sandbox.deploy("v1.4.0")

    assert r.returncode == 0, _out(r)
    assert "--v11" in sandbox.installed_plist()
    assert ">grab<" in sandbox.installed_plist(GRAB)
    assert sandbox.reloads(LABEL) == ["bootout", "bootstrap"]
    assert sandbox.reloads(GRAB) == ["bootout", "bootstrap"]


def test_only_the_changed_job_is_reloaded(sandbox):
    assert sandbox.deploy("v1.4.0").returncode == 0
    (sandbox.root / "launchctl.log").unlink()

    r = sandbox.deploy("v1.5.0")

    assert r.returncode == 0, _out(r)
    assert "--v15" in sandbox.installed_plist(GRAB)
    assert sandbox.reloads(GRAB) == ["bootout", "bootstrap"]
    assert sandbox.reloads(LABEL) == []
    assert "launchd 配置未变" in r.stdout


def test_a_grab_job_that_fails_to_load_reverts_both(sandbox):
    old_main = sandbox.installed_plist()

    r = sandbox.deploy("v1.6.0")

    assert r.returncode == 1, _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.installed_plist() == old_main                  # 主任务的旧 plist 装回去并重新加载
    assert sandbox.reloads(LABEL)[-1] == "bootstrap"
    assert not sandbox.installed(GRAB)                             # 部署前没有抓取任务：卸掉
    assert sandbox.reloads(GRAB)[-1] == "bootout"
    assert sandbox.history()[-1][4:] == ["reverted", "安装 launchd 配置"]


def test_rolling_back_to_a_tag_without_the_grab_job_uninstalls_it(sandbox):
    assert sandbox.deploy("v1.4.0").returncode == 0
    (sandbox.root / "launchctl.log").unlink()

    r = sandbox.deploy("v1.7.0")

    assert r.returncode == 0, _out(r)
    assert not sandbox.installed(GRAB)
    assert sandbox.reloads(GRAB) == ["bootout"]
    assert sandbox.reloads(LABEL) == []
    [bk] = sorted((sandbox.app / "state" / "backups").iterdir())[-1:]
    assert (bk / f"{GRAB}.plist").exists()                        # 卸掉之前留了一份


def test_a_failed_switch_restores_an_uninstalled_grab_job(sandbox):
    assert sandbox.deploy("v1.4.0").returncode == 0
    grab_before = sandbox.installed_plist(GRAB)
    only_in_place = 'case "$PWD" in *media-agent-stage*) exit 0;; *) exit 1;; esac'

    r = sandbox.deploy("v1.7.0", DEPLOY_TEST_CMD=only_in_place)

    assert r.returncode == 1, _out(r)
    assert sandbox.installed_plist(GRAB) == grab_before          # 测试在装 plist 之前就失败了：没动过


def test_redeploying_the_same_tag_installs_a_missing_job(sandbox):
    """旧版的 deploy.sh（只认一个 plist）部署了带抓取任务的 tag：代码是新的，抓取任务没装上。用新脚本再部署同一个
    tag，不能被"已经是这个版本"短路——要把缺的任务装上（生产上 v0.5.x → v0.6.0 的头一次就是这样）。"""
    assert sandbox.deploy("v1.4.0").returncode == 0
    (sandbox.agents_dir / f"{GRAB}.plist").unlink()

    r = sandbox.deploy("v1.4.0")

    assert r.returncode == 0, _out(r)
    assert "无需部署" not in r.stdout
    assert sandbox.installed(GRAB)
    assert sandbox.history()[-1][4] == "ok"


def test_redeploying_with_both_jobs_current_is_a_no_op(sandbox):
    assert sandbox.deploy("v1.4.0").returncode == 0
    n = len(sandbox.history())

    r = sandbox.deploy("v1.4.0")

    assert r.returncode == 0 and "已经是 v1.4.0" in r.stdout
    assert len(sandbox.history()) == n


# ------------------------------------------------------------------ 锁
def test_waits_for_the_run_lock_and_gives_up(sandbox):
    held = RunLock(sandbox.app / "state" / "run.lock", "media-agent run")
    assert held.acquire(wait=0)
    try:
        r = sandbox.deploy("v1.1.0")
    finally:
        held.release()
    assert r.returncode == 1
    assert "拿不到运行锁" in _out(r) and "cmd=media-agent run" in _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.history()[-1][4] == "lock-timeout"


def test_two_deploys_do_not_interleave(sandbox):
    held = RunLock(sandbox.app / "state" / "deploy.lock", "deploy.sh")
    assert held.acquire(wait=0)
    try:
        r = sandbox.deploy("v1.1.0")
    finally:
        held.release()
    assert r.returncode == 1 and "另一个 deploy.sh 正在进行" in _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")


def test_waits_for_a_launchd_round_of_old_lockless_code(sandbox):
    """转换前的老代码不拿运行锁：只能看 launchd 说它是不是正在跑。"""
    r = sandbox.deploy("v1.1.0", FAKE_LAUNCHD_STATE="running")
    assert r.returncode == 1 and "没有切换" in _out(r)
    assert sandbox.head() == sandbox.tag_sha("v1.0.0")
    assert sandbox.history()[-1][4] == "busy"


# ================================================================== convert-to-git.sh
@pytest.fixture
def prod_tree(tmp_path, upstream, tools) -> Sandbox:
    """还不是 git 工作区的"生产目录"：v1.0.0 的文件 + 生产上才有的东西。"""
    bash, uv, launchctl = _tools(tmp_path, tools)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    app = tmp_path / "home" / "media-agent"
    env = {**os.environ, **GIT_ENV, "HOME": str(tmp_path), "SANDBOX": str(tmp_path),
           "TOOLS": str(tools), "TMPDIR": str(tmpdir), "UV_BIN": str(uv),
           "LAUNCHCTL": str(launchctl), "MEDIA_AGENT_HOME": str(app),
           "DEPLOY_TEST_CMD": "test -x .venv/bin/media-agent"}
    env.pop("MEDIA_AGENT_DEPLOYING", None)
    r = subprocess.run([str(bash), "-c", 'mkdir -p "$1" && git --git-dir="$0" archive v1.0.0 '
                        '| tar -x -C "$1"', str(upstream), str(app)], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _write(app / "AGENTS.md", "# 旧版 AGENTS（生产上一直没同步）\n")        # 允许的差异
    (app / "README.md").unlink()                                          # 允许的差异
    _write(app / ".agents" / "notes" / "rejected" / "evolver.md", "# 生产独有\n")
    _write(app / ".agents" / "rules" / "r2.json", '{"id": "r2"}\n')       # 上游后来入库了
    _write(app / ".env", "QBIT_PASS=synthetic\n", 0o600)
    _write(app / ".env.bak-20260917T205051", "QBIT_PASS=real-secret\n", 0o600)
    _write(app / "media_agent" / "core.py.bak-20260915", "old\n")
    _write(app / ".DS_Store", "")
    _write(app / "state" / "trash" / "2026-09-14" / "x.mkv", "trashed\n")
    return Sandbox(tmp_path, app, tmp_path / "LaunchAgents", env, bash)


def _convert(sb: Sandbox, upstream: Path, *args) -> subprocess.CompletedProcess:
    return sb.run(CONVERT, "v1.0.0", "--repo", str(upstream), *args)


def test_convert_turns_the_tree_into_a_checkout_without_changing_runtime_files(prod_tree,
                                                                               upstream):
    app = prod_tree.app
    code_before = (app / "media_agent" / "core.py").read_bytes()

    r = _convert(prod_tree, upstream)

    assert r.returncode == 0, _out(r)
    assert prod_tree.head() == prod_tree.tag_sha("v1.0.0")
    assert (app / "media_agent" / "core.py").read_bytes() == code_before
    assert (app / "AGENTS.md").read_text() == "# AGENTS 1.0.0\n"          # 文档由 tag 落地
    assert (app / "README.md").exists()
    # 只剩未入库的运行时产物；密钥与备份被本机 exclude 挡住
    status = sorted(prod_tree.git("status", "--porcelain", "--untracked-files=all").splitlines())
    assert status == ["?? .agents/notes/rejected/evolver.md", "?? .agents/rules/r2.json"]
    assert "（git 里没有——需要提交）" in r.stdout and "（上游默认分支已有，内容相同）" in r.stdout
    [tgz] = (app / "state" / "harvest").glob("convert-*.tgz")
    with tarfile.open(tgz) as t:
        assert sorted(t.getnames()) == [".agents/notes/rejected/evolver.md",
                                        ".agents/rules/r2.json"]
    # 快照：含 .env、不含 state/，只有属主可读
    [snap] = app.parent.glob("media-agent-pre-git-*.tgz")
    assert snap.stat().st_mode & 0o777 == 0o600
    with tarfile.open(snap) as t:
        names = t.getnames()
    assert "media-agent/.env" in names and not [n for n in names if "/state" in n]
    assert prod_tree.history()[-1][1:5] == ["v1.0.0", prod_tree.tag_sha("v1.0.0"), "pre-git",
                                           "converted"]
    # 之后就能用 deploy.sh 升级：r2.json 与 v1.1.0 相同会被接管，笔记则必须先入库
    r = prod_tree.deploy("v1.1.0", DEPLOY_LOCK_WAIT=1, LAUNCH_AGENTS_DIR=str(prod_tree.agents_dir))
    assert r.returncode == 1 and "evolver.md\tv1.1.0 里没有" in r.stdout
    assert "r2.json" not in r.stdout.split("未入库的文件")[1]


def test_convert_refuses_when_code_differs_from_the_tag(prod_tree, upstream):
    app = prod_tree.app
    _write(app / "media_agent" / "core.py", "VERSION = 'hotfixed on prod'\n")
    before = prod_tree.fingerprint()

    r = _convert(prod_tree, upstream)

    assert r.returncode == 1
    assert "media_agent/core.py" in r.stdout and "生产目录没有任何改动" in r.stdout
    assert not (app / ".git").exists()
    assert prod_tree.fingerprint() == before
    assert not list(app.parent.glob("media-agent-pre-git-*.tgz"))         # 没用上的快照删掉


def test_convert_refuses_while_launchd_runs_a_round(prod_tree, upstream):
    r = prod_tree.run(CONVERT, "v1.0.0", "--repo", str(upstream), FAKE_LAUNCHD_STATE="running")
    assert r.returncode == 1 and "正在跑" in _out(r)
    assert not (prod_tree.app / ".git").exists()


def test_convert_can_be_undone_exactly_as_printed(prod_tree, upstream):
    before = prod_tree.fingerprint()
    r = _convert(prod_tree, upstream)
    assert r.returncode == 0, _out(r)
    lines = r.stdout.split("撤销转换")[1].splitlines()[1:4]
    undo = "\n".join(re.sub(r"\s+#.*$", "", ln).strip() for ln in lines)

    u = subprocess.run([str(prod_tree.bash), "-c", "set -e\n" + undo], env=prod_tree.env,
                       capture_output=True, text=True)

    assert u.returncode == 0, u.stderr
    assert not (prod_tree.app / ".git").exists()
    assert prod_tree.fingerprint() == before


# ================================================================== 脚本本身
SCRIPTS = [DEPLOY, CONVERT, REPO / "deploy" / "vpn-watchdog.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_under_bin_bash(tmp_path, tools, script):
    bash, _, _ = _tools(tmp_path, tools)
    r = subprocess.run([str(bash), "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_bare_variable_right_before_non_ascii_text(script):
    """bash 3.2（生产 macOS）把紧跟在变量名后的多字节字符当成变量名的一部分：
    `"$APP，HEAD"` 会报 `APP\\xef: unbound variable`。一律写成 `${APP}`。"""
    bad = re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])", script.read_text("utf-8"))
    assert bad == []
