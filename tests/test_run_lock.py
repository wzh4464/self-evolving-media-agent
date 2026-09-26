"""运行锁：会改动东西的命令同一时刻只能有一个在跑。

2026-09-26 之前没有任何锁（runloop §5、deploy §3）：launchd 的 `run`、运维手动的
`apply` / `rollback` / `repair` / `purge --apply` 可以同时跑，各拿一份诊断快照
各自执行；部署替换代码时也挡不住正在跑的一轮（critic N17、§3.6）。

锁是 `state/run.lock` 上的 flock——与 deploy.sh 用的 `/usr/bin/lockf -k`
（Linux 上是 `flock(1)`）是同一种锁，下面用真实的第二个进程验证互斥。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from media_agent import cli, runlock
from media_agent.runlock import LOCK_NAME, RunLock

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def lock_path(project_root) -> Path:
    return project_root / "state" / LOCK_NAME          # = load_config().state_dir / run.lock


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(runlock, "DEFAULT_WAIT", 0.2)


# ------------------------------------------------------------------ 两个持有者
def test_second_holder_is_refused_and_told_who_holds_it(lock_path):
    a, b = RunLock(lock_path, "media-agent run"), RunLock(lock_path, "media-agent apply")

    assert a.acquire(wait=0)
    assert not b.acquire(wait=0)
    assert f"pid={os.getpid()}" in b.holder() and "cmd=media-agent run" in b.holder()

    a.release()
    assert b.acquire(wait=0)
    assert "cmd=media-agent apply" in a.holder()
    b.release()
    assert lock_path.exists()                            # 锁文件永不删除（见 runlock 模块注释）
    assert lock_path.read_text() == ""                   # 释放时清掉自述


def test_short_wait_then_give_up(lock_path):
    a = RunLock(lock_path, "a")
    assert a.acquire(wait=0)
    t0 = time.monotonic()
    assert not RunLock(lock_path, "b").acquire(wait=0.3)
    assert 0.25 <= time.monotonic() - t0 < 2
    a.release()


def test_waiter_gets_the_lock_once_the_holder_leaves(lock_path, tmp_path):
    holder = _Holder(tmp_path, lock_path)
    holder.start()
    waiter = RunLock(lock_path, "waiter")
    assert not waiter.acquire(wait=0)
    holder.stop_later(0.3)
    assert waiter.acquire(wait=5)                        # 短暂等待吸收"对方正在退出"
    waiter.release()


def test_relocked_inode_is_detected(lock_path, monkeypatch):
    """锁住的是刚被人删掉重建的旧 inode 时，那把锁不排斥任何人——必须重来。"""
    import fcntl
    real = fcntl.flock
    calls = {"n": 0}

    def swap_then_lock(fd, op):
        calls["n"] += 1
        if calls["n"] == 1:                              # 打开之后、上锁之前，路径被换掉
            lock_path.unlink()
            lock_path.write_text("")
        return real(fd, op)

    lock = RunLock(lock_path, "x")
    monkeypatch.setattr(fcntl, "flock", swap_then_lock)
    assert lock.acquire(wait=0)
    monkeypatch.setattr(fcntl, "flock", real)
    assert calls["n"] == 2
    assert os.fstat(lock._fd).st_ino == os.stat(lock_path).st_ino
    assert not RunLock(lock_path, "y").acquire(wait=0)   # 锁的确实是路径上的那个文件
    lock.release()


# ------------------------------------------------------------------ 真实的第二个进程
HOLDER_SRC = """\
import sys
from pathlib import Path
from media_agent.runlock import RunLock

lock = RunLock(sys.argv[1], "holder-under-test")
assert lock.acquire(wait=5), "holder could not lock"
Path(sys.argv[2]).write_text("ready")
sys.stdin.read()                 # 父进程关 stdin 就退出（或被 kill）
lock.release()
"""


class _Holder:
    """另一个进程里持锁。脚本放在测试自己的 tmp 目录（子进程守卫只放行那里）。"""

    def __init__(self, tmp_path: Path, lock_path: Path):
        self.script = tmp_path / "bin" / "hold-lock"
        self.script.parent.mkdir(parents=True, exist_ok=True)
        self.script.write_text(f"#!{sys.executable}\n{HOLDER_SRC}", encoding="utf-8")
        self.script.chmod(0o755)
        self.ready = tmp_path / "holder.ready"
        self.lock_path = lock_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> "_Holder":
        env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
        self.proc = subprocess.Popen([str(self.script), str(self.lock_path), str(self.ready)],
                                     stdin=subprocess.PIPE, env=env)
        deadline = time.monotonic() + 10
        while not self.ready.exists():
            assert self.proc.poll() is None, "持锁进程提前退出了"
            assert time.monotonic() < deadline, "持锁进程 10 秒内没拿到锁"
            time.sleep(0.02)
        return self

    def stop_later(self, delay: float) -> None:
        import threading
        threading.Timer(delay, self.stop).start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(10)

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(10)


@pytest.fixture
def holder(tmp_path, lock_path):
    h = _Holder(tmp_path, lock_path)
    yield h
    if h.proc and h.proc.poll() is None:
        h.kill()


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["media-agent", *argv])
    return cli.main()


def test_run_exits_75_while_another_process_holds_the_lock(holder, monkeypatch, capsys, fast):
    holder.start()
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: pytest.fail("持锁期间不该开跑"))

    rc = _main(monkeypatch, "run")

    assert rc == cli.EXIT_LOCKED == 75
    out = capsys.readouterr()
    assert "另一个 media-agent 进程正持有运行锁" in out.out
    assert f"pid={holder.proc.pid}" in out.out and "holder-under-test" in out.out


def test_lock_dies_with_its_process(holder, lock_path, monkeypatch, fast):
    """flock 随进程释放：被 kill 的一轮不会留下一把要人手动删的锁。"""
    holder.start()
    holder.kill()
    ran = []
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: ran.append(1) or 0)

    assert _main(monkeypatch, "run") == 0
    assert ran == [1]


# ------------------------------------------------------------------ 哪些命令拿锁
LOCKED = [
    (["run"], "cmd_run"),
    (["run", "--dry-run", "--no-evolve"], "cmd_run"),
    (["apply"], "cmd_apply"),
    (["apply", "--dry-run"], "cmd_apply"),        # 预演也写审计、也在读正在变的库
    (["rollback", "--last"], "cmd_rollback"),
    (["rollback", "--run", "20260920T170126", "--dry-run"], "cmd_rollback"),
    (["repair", "--run", "20260920T170126"], "cmd_repair"),
    (["purge", "--apply"], "cmd_purge"),
    (["evolve"], "cmd_evolve"),
]
UNLOCKED = [
    (["scan"], "cmd_scan"),
    (["diagnose"], "cmd_diagnose"),
    (["runs"], "cmd_runs"),
    (["purge"], "cmd_purge"),                       # 只预演：不删任何东西
]


@pytest.mark.parametrize("argv,func", LOCKED, ids=[" ".join(a) for a, _ in LOCKED])
def test_mutating_commands_are_locked_out(argv, func, lock_path, monkeypatch, fast):
    other = RunLock(lock_path, "media-agent run")
    assert other.acquire(wait=0)
    monkeypatch.setattr(cli, func, lambda args, cfg: pytest.fail(f"{func} 不该在锁被占时运行"))
    try:
        assert _main(monkeypatch, *argv) == cli.EXIT_LOCKED
    finally:
        other.release()


@pytest.mark.parametrize("argv,func", LOCKED, ids=[" ".join(a) for a, _ in LOCKED])
def test_mutating_commands_hold_the_lock_while_running_and_release_it(argv, func, lock_path,
                                                                     monkeypatch):
    seen = {}

    def body(args, cfg):
        probe = RunLock(lock_path, "probe")
        seen["locked"] = not probe.acquire(wait=0)
        seen["holder"] = probe.holder()
        return 0

    monkeypatch.setattr(cli, func, body)
    assert _main(monkeypatch, *argv) == 0
    assert seen["locked"], "命令执行期间锁必须是被占着的"
    assert f"cmd=media-agent {' '.join(argv)}" in seen["holder"]
    assert RunLock(lock_path, "after").acquire(wait=0)  # 返回后已释放


@pytest.mark.parametrize("argv,func", UNLOCKED, ids=[" ".join(a) for a, _ in UNLOCKED])
def test_read_only_commands_run_while_locked(argv, func, lock_path, monkeypatch, fast):
    other = RunLock(lock_path, "media-agent run")
    assert other.acquire(wait=0)
    ran = []
    monkeypatch.setattr(cli, func, lambda args, cfg: ran.append(func) or 0)
    try:
        assert _main(monkeypatch, *argv) == 0
    finally:
        other.release()
    assert ran == [func]


def test_lock_is_released_when_the_command_crashes(lock_path, monkeypatch):
    def boom(args, cfg):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(cli, "cmd_apply", boom)
    with pytest.raises(RuntimeError):
        _main(monkeypatch, "apply")
    assert RunLock(lock_path, "after").acquire(wait=0)


# ------------------------------------------------------------------ 与 shell 工具互斥
def _shell_locker():
    if Path("/usr/bin/lockf").exists():              # macOS / BSD（生产 zihan_air）
        return "/usr/bin/lockf", lambda path, *cmd: ["-k", "-s", "-t", "0", str(path), *cmd]
    flock = shutil.which("flock")                    # Linux util-linux
    if flock:
        return flock, lambda path, *cmd: ["-n", "-E", "75", str(path), *cmd]
    return None, None


@pytest.fixture
def shell_locker(tmp_path):
    exe, argv = _shell_locker()
    if exe is None:
        pytest.skip("没有 lockf(1) / flock(1)")
    wrapper = tmp_path / "bin" / "shell-lock"          # 子进程守卫只放行 tmp 里的可执行文件
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(f'#!/bin/sh\nexec {exe} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper, argv


def test_shell_lock_tool_is_refused_while_python_holds_it(lock_path, shell_locker):
    """deploy.sh 用 lockf/flock 拿的是同一把锁：一轮 run 在跑时它拿不到。"""
    wrapper, argv = shell_locker
    held = RunLock(lock_path, "media-agent run")
    assert held.acquire(wait=0)
    r = subprocess.run([str(wrapper), *argv(lock_path, "/usr/bin/true")])
    assert r.returncode == 75
    held.release()
    r = subprocess.run([str(wrapper), *argv(lock_path, "/usr/bin/true")])
    assert r.returncode == 0
    assert lock_path.exists()                          # -k / flock 都不删锁文件


def test_python_is_refused_while_the_shell_tool_holds_it(lock_path, shell_locker, tmp_path):
    """反过来：部署切换代码期间，launchd 起来的 run 进不来。"""
    wrapper, argv = shell_locker
    ready = tmp_path / "shell.ready"
    lock_path.parent.mkdir(parents=True, exist_ok=True)  # lockf 只建文件、不建目录
    proc = subprocess.Popen(
        [str(wrapper), *argv(lock_path, "/bin/sh", "-c", 'echo ready > "$0"; read x', str(ready))],
        stdin=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        assert not RunLock(lock_path, "media-agent run").acquire(wait=0)
    finally:
        proc.stdin.close()
        proc.wait(10)
    assert RunLock(lock_path, "media-agent run").acquire(wait=0)
