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


# ------------------------------------------------------------------ 抓取模式的任务
GRAB = PLIST.with_name("com.zihan.media-agent-grab.plist")


def _load_grab() -> dict:
    with GRAB.open("rb") as fh:
        return plistlib.load(fh)


def test_grab_job_runs_the_grab_subcommand_every_half_hour():
    """`media-agent grab` 每 30 分钟一次（`grabmode.GRAB_INTERVAL_S`），加载时不跑；同一个 venv 入口、同一个工作目录。"""
    from media_agent import cache, grabmode

    g, main = _load_grab(), _load()
    assert g["Label"] == "com.zihan.media-agent-grab"
    assert g["ProgramArguments"] == [main["ProgramArguments"][0], "grab"]
    assert g["WorkingDirectory"] == main["WorkingDirectory"]
    assert g["StartInterval"] == grabmode.GRAB_INTERVAL_S == 1800 and g["RunAtLoad"] is False
    assert cache.FEED_TTL < g["StartInterval"]       # 每一次抓取都看到新拉的番组页 feed


def test_grab_job_is_as_gentle_as_the_main_one_and_logs_apart():
    g, main = _load_grab(), _load()
    assert (g["Nice"], g["LowPriorityIO"]) == (main["Nice"], main["LowPriorityIO"])
    home = Path(g["WorkingDirectory"])
    assert Path(g["StandardOutPath"]) == home / "state" / "grab.log"
    assert Path(g["StandardErrorPath"]) == home / "state" / "grab.err.log"
    from media_agent import runlog
    assert runlog.GRAB_LOG_NAMES == ("grab.log", "grab.err.log")
