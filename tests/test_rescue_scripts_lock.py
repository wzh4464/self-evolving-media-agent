"""`deploy/rescue.py` 与 `deploy/vpn-watchdog.sh` 重建 qBittorrent 容器时拿 media-agent 的运行锁（critic N17）。

两个脚本都会 `docker compose up -d --force-recreate`：qBittorrent 在一轮 `run` 中途消失，扫描读到一半、改名改到一半
（第 1 阶段的"读不全就整批拒绝"只挡得住扫描那一刻）。现在它们在重建之前拿 `state/run.lock`（与 media-agent、
deploy.sh 同一把 flock），最多等 `RESCUE_LOCK_WAIT` / `RUNLOCK_WAIT`（默认 900 秒，一轮 run 约 2 分钟）；等不到
就不重建、以 75 结束、说清谁占着——下次再来。

这里只测仓库里的副本；生产用的是 `~/gluetun/` 下的拷贝，同步步骤见 deploy/README.md。不联网、不碰 docker：
rescue.py 的 docker / qBittorrent 调用换成替身，watchdog 用一个替身 docker 脚本。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_agent.runlock import LOCK_NAME, RunLock

REPO = Path(__file__).resolve().parent.parent
RESCUE = REPO / "deploy" / "rescue.py"
WATCHDOG = REPO / "deploy" / "vpn-watchdog.sh"


# ================================================================== rescue.py
class FakeQ:
    def __init__(self, log):
        self.log = log

    def torrents(self):
        return [{"hash": "a" * 40, "progress": 1.0, "state": "stalledUP", "name": "x"}]

    def _post(self, path, data=None):
        self.log.append(("post", path))

    def _get(self, path):
        if "preferences" in path:
            return {"max_ratio_enabled": False, "max_ratio": -1, "max_ratio_act": 0}
        return {"connection_status": "connected", "dht_nodes": 1}


@pytest.fixture
def rescue(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "gluetun").mkdir(parents=True)
    ma = tmp_path / "ma"
    (ma / "state").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MEDIA_AGENT_HOME", str(ma))
    monkeypatch.setenv("RESCUE_LOCK_WAIT", "0.3")
    spec = importlib.util.spec_from_file_location("rescue_under_test", RESCUE)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "path", list(sys.path))            # 它会往 sys.path 里插 MEDIA_AGENT_HOME
    spec.loader.exec_module(mod)

    calls: list = []
    lock_path = ma / "state" / LOCK_NAME

    def compose(vps):
        probe = RunLock(lock_path, "probe")
        held = not probe.acquire(wait=0)
        if not held:
            probe.release()
        calls.append(("compose", vps, "held" if held else "free"))
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(mod, "compose", compose)
    monkeypatch.setattr(mod, "wait_healthy", lambda limit=150: True)
    monkeypatch.setattr(mod, "exit_ip", lambda: "?")
    monkeypatch.setattr(mod, "health", lambda: "healthy")
    monkeypatch.setattr(mod, "qbit", lambda: FakeQ(calls))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return SimpleNamespace(mod=mod, calls=calls, lock=lock_path, marker=home / "gluetun" / ".rescue-active")


def test_rescue_start_recreates_qbit_only_while_holding_the_run_lock(rescue, capsys):
    assert rescue.mod.cmd_start() == 0
    assert [c for c in rescue.calls if c[0] == "compose"] == [("compose", True, "held")]
    assert rescue.marker.exists()
    probe = RunLock(rescue.lock, "after")
    assert probe.acquire(wait=0)                                    # 用完就放
    probe.release()


def test_rescue_start_backs_off_when_media_agent_is_running(rescue, capsys):
    busy = RunLock(rescue.lock, "media-agent run")
    assert busy.acquire(wait=0)
    try:
        rc = rescue.mod.cmd_start()
    finally:
        busy.release()

    out = capsys.readouterr().out
    assert rc == 75
    assert rescue.calls == []                                       # 没暂停做种、没重建
    assert not rescue.marker.exists()
    assert "media-agent run" in out and "稍后" in out


def test_rescue_auto_backs_off_with_75_when_media_agent_is_running(rescue, capsys):
    """`auto` 先 `start`：等不到锁就以 75 结束、不进入等待（复审变异 H9l：返回 1 全套照绿——launchd / 人看退出码
    分不出"被挡住了、稍后再来"与"出错了"）。"""
    busy = RunLock(rescue.lock, "media-agent run")
    assert busy.acquire(wait=0)
    try:
        rc = rescue.mod.cmd_auto(0.01)
    finally:
        busy.release()

    assert rc == 75
    assert rescue.calls == [] and not rescue.marker.exists()
    assert "进入等待" not in capsys.readouterr().out


def test_rescue_stop_recreates_only_while_holding_the_lock_and_clears_the_marker(rescue, capsys):
    rescue.mod.cmd_start()
    rescue.calls.clear()

    assert rescue.mod.cmd_stop() == 0

    assert [c for c in rescue.calls if c[0] == "compose"] == [("compose", False, "held")]
    assert not rescue.marker.exists()


def test_failed_start_rolls_back_inside_the_same_lock(rescue, capsys, monkeypatch):
    """start 的 compose 失败时它在锁里调用 stop 回滚：同一个进程重入，不能自己把自己锁死。"""
    real = rescue.mod.compose

    def flaky(vps):
        real(vps)
        return subprocess.CompletedProcess([], 1 if vps else 0, "", "boom")

    monkeypatch.setattr(rescue.mod, "compose", flaky)

    rescue.mod.cmd_start()

    assert [c for c in rescue.calls if c[0] == "compose"] == [("compose", True, "held"),
                                                               ("compose", False, "held")]
    assert not rescue.marker.exists()


# ================================================================== vpn-watchdog.sh
FAKE_DOCKER = r"""#!/bin/sh
# 替身 docker：health 从文件读；compose 记一笔（顺带看运行锁此刻是否被人拿着），然后变健康
D="$FAKE_DOCKER_DIR"
case "$1" in
  info) exit 0 ;;
  inspect) cat "$D/health" ;;
  logs) echo "Public IP address is ?" ;;
  compose)
    if "$PYTHON" -c 'import fcntl,os,sys
fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT)
try:
    fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
sys.exit(0)' "$RUNLOCK_PATH"; then echo "compose free" >> "$D/calls"; else echo "compose held" >> "$D/calls"; fi
    echo healthy > "$D/health" ;;
esac
exit 0
"""


@pytest.fixture
def watchdog(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    bash = bindir / "bash"
    bash.write_text('#!/bin/sh\nexec /bin/bash "$@"\n', encoding="utf-8")
    bash.chmod(0o755)
    d = tmp_path / "docker"
    d.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    (d / "health").write_text("unhealthy\n")
    home = tmp_path / "home"
    (home / "gluetun").mkdir(parents=True)
    ma = tmp_path / "ma"
    (ma / "state").mkdir(parents=True)
    lock = ma / "state" / LOCK_NAME
    env = {**os.environ, "HOME": str(home), "MEDIA_AGENT_HOME": str(ma), "DOCKER_BIN": str(docker),
           "FAKE_DOCKER_DIR": str(d), "RUNLOCK_PATH": str(lock), "PYTHON": sys.executable,
           "WATCHDOG_POLL": "0", "RUNLOCK_WAIT": "1"}
    env.pop("MA_RUNLOCK_HELD", None)

    def run():
        return subprocess.run([str(bash), str(WATCHDOG)], env=env, capture_output=True, text=True,
                              timeout=60)

    def calls():
        p = d / "calls"
        return p.read_text().split("\n")[:-1] if p.exists() else []

    return SimpleNamespace(run=run, calls=calls, lock=lock,
                           log=home / "gluetun" / "vpn-watchdog.log")


pytestmark_bash = pytest.mark.skipif(not Path("/bin/bash").exists(), reason="需要 /bin/bash")


@pytestmark_bash
def test_watchdog_recreates_while_holding_the_run_lock(watchdog):
    r = watchdog.run()
    assert r.returncode == 0, r.stderr
    assert watchdog.calls() == ["compose held"]
    assert "RECOVERED" in watchdog.log.read_text()


@pytestmark_bash
def test_watchdog_skips_the_recreate_while_media_agent_runs(watchdog):
    busy = RunLock(watchdog.lock, "media-agent run")
    assert busy.acquire(wait=0)
    try:
        r = watchdog.run()
    finally:
        busy.release()
    assert r.returncode == 75, r.stderr
    assert watchdog.calls() == []
    log = watchdog.log.read_text()
    assert "SKIP" in log and "media-agent run" in log


@pytestmark_bash
def test_watchdog_healthy_tunnel_touches_nothing(watchdog, tmp_path):
    (tmp_path / "docker" / "health").write_text("healthy\n")
    r = watchdog.run()
    assert r.returncode == 0 and watchdog.calls() == []


def test_watchdog_reads_docker_from_the_environment():
    """deploy/README 一直说 DOCKER_BIN 可配，脚本却写死了路径。"""
    text = WATCHDOG.read_text(encoding="utf-8")
    assert 'DOCKER="${DOCKER_BIN:-/usr/local/bin/docker}"' in text
