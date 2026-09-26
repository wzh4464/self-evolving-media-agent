"""launchd 定时任务的形状（deploy/com.zihan.media-agent.plist）。

critic §3.7：以前是 `uv run --directory ~/media-agent media-agent run`。`uv run` 每轮都
按 uv.lock 同步环境——锁文件一变（引入 pytest 那次就变了），凌晨那一轮就要联网装依赖，
还得指望生产的 uv 0.7.2 读得懂新锁文件。现在直接跑 venv 里的入口，依赖只在部署时装。
"""
from __future__ import annotations

import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parent.parent / "deploy" / "com.zihan.media-agent.plist"


def _load() -> dict:
    with PLIST.open("rb") as fh:
        return plistlib.load(fh)


def test_runs_the_venv_entrypoint_without_uv():
    p = _load()
    prog = p["ProgramArguments"]
    assert prog[0].endswith("/media-agent/.venv/bin/media-agent")
    assert prog[1:] == ["run"]
    assert not any(Path(a).name == "uv" for a in prog)


def test_schedule_priority_and_logs_are_kept():
    p = _load()
    home = Path(p["WorkingDirectory"])
    assert Path(p["ProgramArguments"][0]).is_relative_to(home)
    assert p["StartInterval"] == 21600 and p["RunAtLoad"] is False
    assert p["Nice"] == 10 and p["LowPriorityIO"] is True
    assert Path(p["StandardOutPath"]) == home / "state" / "run.log"
    assert Path(p["StandardErrorPath"]) == home / "state" / "run.err.log"
    assert p["Label"] == "com.zihan.media-agent"
