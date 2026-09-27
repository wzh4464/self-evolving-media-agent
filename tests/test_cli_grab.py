"""`media-agent grab`：launchd 每 30 分钟一次的抓取模式（`grabmode`），与 6 小时的 `run` 共用一把运行锁。

- 拿锁只等一小会儿（`runlock.DEFAULT_WAIT`），被占就以 75 结束——**绝不为一轮 `run` 等上几分钟**，30 分钟后自然再来；
  反过来 `run` 等得久一点（`runlock.RUN_WAIT`）：两个任务按同一个起点计时，每 6 小时撞一次，抓取一般一两分钟就完，
  `run` 不该因此整整晚 6 小时。
- 维护暂停照样认（VPN 救援 / `state/PAUSE`，75）。
- 健康报告写 `state/health/grab/`（`cmd: grab`）：不挤掉 `run` 的报告、`media-agent health` 默认仍看 `run` 的（`--grab`
  看抓取的）；通知只为抓取相关的事（崩溃、整批拒绝、审计没写全、抓取动作失败 / 未确认、规则崩了、TMDB 没配），
  "锁被 run 占着"、"维护暂停"这类抓取的日常不发信；去重状态单独一份（`notify-grab.json`），与 `run` 的互不干扰。
- 不写发现历史：每 30 分钟一份会把 `run` 的快照挤出保留窗口，卡住检测只数 `run`。
- 自己的日志 `state/grab.log` / `grab.err.log`，与 run.log 一样轮转、每行带时间与批次 ID。
- 番组页 feed 的缓存比抓取的节奏短：每一次抓取看到的都是新拉的 feed。
"""
from __future__ import annotations

import json
import sys
import threading

import pytest
from harness import MikanItem, video, weekly

from media_agent import cache, cli, grabmode, health, notify, runlock, runlog
from media_agent.runlock import LOCK_NAME, RunLock

SHOW = "抓取庚"
TMDB = 3801
MID = "5001"
TPL = "[LoliHouse] 抓取庚 / Zhuaqu Geng - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    monkeypatch.setattr(cli, "load_config", lambda: lib.cfg)
    return lib


def _scene(lib):
    sched = weekly(12, first_days_ago=30)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in (1, 2, 3, 4):
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID,
               seasons={"1": {"have": [1, 2, 3, 4]}})
    lib.configure(qbit_allow_empty=True)
    lib.mikan(MID, [MikanItem(title=TPL.format(5), pub=dict(sched)[5])], search=[SHOW])


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["media-agent", *argv])
    return cli.main()


def _grab_reports(lib) -> list[dict]:
    d = health.health_dir(lib.cfg.state_dir, "grab")
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.exists() else []


def test_grab_grabs_and_writes_its_own_health_report(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    _scene(lib)

    rc = _main(monkeypatch, "grab")

    out = capsys.readouterr().out
    assert rc == 0, out
    [rec] = [r for r in lib.audit() if r["op"] == "grab_episode"]
    assert rec["status"] == "applied" and rec["args"]["episode"] == 5
    [rep] = _grab_reports(lib)
    assert rep["cmd"] == "grab" and rep["status"] == "ok" and rep["grab"]["applied"] == 1
    assert rep["run_id"] == rec["run_id"]
    assert health.load_report(lib.cfg.state_dir) is None           # `run` 的报告不被挤占
    assert not (lib.cfg.state_dir / "findings").exists()            # 不写发现历史
    assert "抓取" in out and "S1E05" in out


def test_grab_prints_only_findings_that_concern_it(offline_cli, monkeypatch, capsys):
    """AB 的发布名文件（`run` 会改名）照样被诊断出来，但抓取的输出与健康报告里不列它。"""
    lib = offline_cli
    _scene(lib)
    lib.show(SHOW).season(1).single("[ANi] Zhuaqu Geng - 06 [1080P].mp4", tags="ab:9",
                                    probe=video("h264", subs=["chi 简体中文"]))

    assert _main(monkeypatch, "grab") == 0

    out = capsys.readouterr().out
    assert "Zhuaqu Geng - 06" not in out
    [rep] = _grab_reports(lib)
    assert rep["grab"]["applied"] == 1
    assert rep["findings"]["total"] == 0          # 收尾诊断里只剩 AB 那一份的改名：不是抓取的事，不算


def test_grab_refuses_loudly_when_qbittorrent_is_down(offline_cli, monkeypatch, capsys):
    """qBittorrent 不可用：整批拒绝，大声说（stdout 进 grab.log、stderr 进 grab.err.log），退出码 3、健康报告 critical（与
    `run` 同一个口径）。审查的变异 T4-19（拒绝时 `return 0`）全套测试照样过：退出码由健康报告的 critical 原因兜住了，
    少的是那一行拒绝的横幅——没有测试看它。"""
    lib = offline_cli
    _scene(lib)
    lib.qbit_down()

    assert _main(monkeypatch, "grab") == cli.EXIT_DEGRADED

    captured = capsys.readouterr()
    assert "⛔ 拒绝执行任何改动" in captured.out and "⛔ 拒绝执行任何改动" in captured.err
    [rep] = _grab_reports(lib)
    assert rep["status"] == "critical"
    assert not [r for r in lib.audit() if r["op"] == "grab_episode"]


def test_health_shows_the_last_grab_with_a_flag(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    _scene(lib)
    _main(monkeypatch, "grab")
    capsys.readouterr()

    assert _main(monkeypatch, "health") == 1                         # 还没有 run 的报告
    assert _main(monkeypatch, "health", "--grab") == 0
    assert "抓取" in capsys.readouterr().out


def test_grab_gives_up_quickly_when_the_run_holds_the_lock(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    monkeypatch.setattr(runlock, "DEFAULT_WAIT", 0.1)
    monkeypatch.setattr(runlock, "RUN_WAIT", 30.0)                  # 抓取不能按 run 的等法等
    monkeypatch.setattr(cli, "cmd_grab", lambda args, cfg: pytest.fail("锁被占时不该开抓"))
    held = RunLock(lib.cfg.state_dir / LOCK_NAME, "media-agent run")
    assert held.acquire(wait=0)
    try:
        assert _main(monkeypatch, "grab") == cli.EXIT_LOCKED
    finally:
        held.release()
    [rep] = _grab_reports(lib)
    assert rep["status"] == "warn" and rep["reasons"][0]["code"] == "locked"
    assert "media-agent run" in rep["reasons"][0]["text"]


@pytest.mark.parametrize("cmd, wait", [("grab", "DEFAULT_WAIT"), ("run", "RUN_WAIT")])
def test_each_command_waits_for_the_lock_as_long_as_it_should(offline_cli, monkeypatch, cmd, wait):
    """抓取拿锁只短等（`DEFAULT_WAIT`），`run` 等得久（`RUN_WAIT`）。上一条测试只看退出码 75：抓取要是按 `RUN_WAIT` 等，
    照样 75、只是晚几十秒——审查的变异 T4-10b 全套测试照样过。这里直接看传给 `acquire` 的等待时长。"""
    waits = []
    real = RunLock.acquire
    monkeypatch.setattr(RunLock, "acquire", lambda self, wait=runlock.DEFAULT_WAIT: waits.append(wait) or real(self, 0))
    monkeypatch.setattr(cli, f"cmd_{cmd}", lambda args, cfg: 0)

    assert _main(monkeypatch, cmd) == 0

    assert waits == [getattr(runlock, wait)]


def test_run_waits_much_longer_than_a_grab_takes():
    """两个任务按同一个起点计时、每 6 小时撞一次：`run` 要等得过一轮抓取（一般一两分钟），抓取绝不为 `run` 等几分钟。
    审查的变异 T4-17（`RUN_WAIT = 10.0`，与抓取一样短）全套测试照样过——每个测试都把它改小了。"""
    assert runlock.RUN_WAIT >= 10 * runlock.DEFAULT_WAIT
    assert runlock.RUN_WAIT >= 180


def test_run_waits_out_a_grab_that_holds_the_lock(offline_cli, monkeypatch):
    lib = offline_cli
    monkeypatch.setattr(runlock, "DEFAULT_WAIT", 0.1)
    monkeypatch.setattr(runlock, "RUN_WAIT", 10.0)
    ran = []
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: ran.append(1) or 0)
    held = RunLock(lib.cfg.state_dir / LOCK_NAME, "media-agent grab")
    assert held.acquire(wait=0)
    threading.Timer(0.5, held.release).start()

    assert _main(monkeypatch, "run") == 0
    assert ran == [1]


def test_grab_honours_the_maintenance_pause(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    (lib.cfg.state_dir).mkdir(parents=True, exist_ok=True)
    (lib.cfg.state_dir / "PAUSE").write_text("换硬盘\n", encoding="utf-8")
    monkeypatch.setattr(cli, "cmd_grab", lambda args, cfg: pytest.fail("暂停时不该开抓"))

    assert _main(monkeypatch, "grab") == cli.EXIT_LOCKED

    [rep] = _grab_reports(lib)
    assert rep["status"] == "warn" and "换硬盘" in rep["paused"]


def test_grab_rotates_its_own_logs_not_the_runs(offline_cli, monkeypatch):
    lib = offline_cli
    monkeypatch.setattr(cli, "cmd_grab", lambda args, cfg: 0)
    sd = lib.cfg.state_dir
    sd.mkdir(parents=True, exist_ok=True)
    big = runlog.MAX_BYTES + 1
    for name in ("grab.log", "run.log"):
        with open(sd / name, "wb") as fh:            # 稀疏文件：超过轮转阈值，瞬间建好
            fh.truncate(big)

    assert _main(monkeypatch, "grab") == 0

    assert (sd / "grab.log.1").stat().st_size == big and (sd / "grab.log").stat().st_size == 0
    assert not (sd / "run.log.1").exists() and (sd / "run.log").stat().st_size == big


# ------------------------------------------------------------------ 通知
def test_a_locked_out_grab_does_not_mail_and_keeps_its_own_notify_state(offline_cli, monkeypatch):
    """抓取的通知要经 `_finish_run` 按 `scope=grab` 发：锁被 `run` 占着是抓取的日常，不发信；去重状态写
    `notify-grab.json`、不碰 `run` 的 `notify.json`。只直接调 `notify.maybe_send(scope="grab")` 的测试拦不住调用处退回默认的
    `run` 口径（审查的变异 T4-16，全套测试照样过）——那样每 6 小时撞锁一次就发一封，两边的去重状态互相覆盖。"""
    lib = offline_cli
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, msg: sent.append(msg))
    lib.configure(notify_email_to="you@example.com", notify_smtp_host="smtp.example.com")
    monkeypatch.setattr(runlock, "DEFAULT_WAIT", 0.1)
    held = RunLock(lib.cfg.state_dir / LOCK_NAME, "media-agent run")
    assert held.acquire(wait=0)
    try:
        assert _main(monkeypatch, "grab") == cli.EXIT_LOCKED
    finally:
        held.release()

    assert sent == []
    assert (lib.cfg.state_dir / "notify-grab.json").exists()
    assert not (lib.cfg.state_dir / "notify.json").exists()


@pytest.mark.parametrize("reasons, mailed", [
    ([{"level": "warn", "code": "locked", "text": "运行锁被占着"}], False),
    ([{"level": "warn", "code": "paused", "text": "维护暂停"}], False),
    ([{"level": "warn", "code": "unrenamed_old", "text": "未改名"}], False),
    ([{"level": "warn", "code": "failed_actions", "text": "1 个动作失败"}], True),
    ([{"level": "critical", "code": "crash", "text": "本轮崩溃"}], True),
])
def test_grab_mails_only_grab_relevant_events(lib, monkeypatch, reasons, mailed):
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, msg: sent.append(msg))
    lib.configure(notify_email_to="you@example.com", notify_smtp_host="smtp.example.com")
    lib.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    level = "critical" if any(r["level"] == "critical" for r in reasons) else "warn"
    rep = {"run_id": "g1", "cmd": "grab", "status": level, "reasons": reasons, "finished": "2026-09-27T10:00:00",
           "stuck": None, "degraded": {"refused": ""},
           "actions": {"applied": 0, "skipped": 0, "failed": 0, "unknown": 0, "audit_problems": 0}}

    notify.maybe_send(lib.cfg, rep, scope="grab")

    assert bool(sent) is mailed
    if mailed:
        assert "抓取" in sent[0]["Subject"]
    assert (lib.cfg.state_dir / "notify-grab.json").exists()
    assert not (lib.cfg.state_dir / "notify.json").exists()


def test_the_feed_cache_is_shorter_than_the_grab_cadence():
    """每一次抓取都要看到新拉的番组页 feed；但一轮之内的几次迭代（几十秒）要走缓存。"""
    assert 5 * 60 <= cache.FEED_TTL < grabmode.GRAB_INTERVAL_S


def test_grab_is_wired_like_run(monkeypatch):
    """`grab` 拿锁、认维护暂停；`--dry-run` 与全局的 `--no-tmdb` 都在。"""
    captured = {}

    def fake(args, cfg):
        captured.update(lock=args.lock, pause=args.pause, dry=args.dry_run, no_tmdb=args.no_tmdb)
        return 0
    monkeypatch.setattr(cli, "cmd_grab", fake)
    monkeypatch.setattr(sys, "argv", ["media-agent", "--no-tmdb", "grab", "--dry-run"])
    assert cli.main() == 0
    assert captured == {"lock": True, "pause": True, "dry": True, "no_tmdb": True}
