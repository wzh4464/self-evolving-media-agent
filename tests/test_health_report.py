"""每轮健康报告：`run` 收尾（正常、整批拒绝、异常冲出都一样）写 `state/health/<批次 ID>.json`，并打印一小节。

以前 `run` 的输出只有发现清单与一行"执行 N 项"；launchd 上 last exit code 常年是 0。2026-09-16 … 09-26 抓取
0 次成功（12 次 NameError 全在 audit.jsonl 里），qBit 登录超时的轮次照常执行，都要事后翻审计才知道。

总体状态：
- **critical**（`run` 退出码非零，launchd 的 last exit code 看得见）：异常冲出（1）、整批拒绝（3：qBittorrent
  不可用 / 读不全 / 种子数骤降，或演进重扫时读不全）、审计没能原样写进 audit.jsonl（4）、处置之后媒体卷剩余
  仍低于 MIN_FREE_GB（5，新）。几种同时出现时取靠前的那个退出码。
- **warn**（退出码 0）：检测器崩了、有 failed / unknown 的动作、有未确认的卡住问题、发布名文件超过
  UNRENAMED_ALERT_HOURS 还没改名、AutoBangumi 连不上、TMDB 没配、空间低过阈值但提前删回来了、读不到剩余空间、
  隔离区处置有删除失败。
- 其余 **ok**。
"""
from __future__ import annotations

import argparse
import json

import pytest

from media_agent import cli, disposal, health, history
from media_agent.config import load_config
from media_agent.kernel import Action, Finding, Registry

GB = 600_000_000


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True             # 与生产一致：配了 TMDB（没配是一条 warn，见下）
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _clean(lib):
    s1 = lib.show("测试番").season(1)
    s1.single("测试番 S01E01.mkv", size=GB, name="[G] Test Show - 01 [1080p].mkv")


def _latest(lib) -> dict:
    return health.load_report(lib.cfg.state_dir)


# ------------------------------------------------------------------ 正常一轮
def test_a_clean_run_writes_an_ok_report_and_prints_a_section(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)

    rc = cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr().out
    rep = _latest(lib)
    assert rc == 0 and rep["status"] == "ok" and rep["exit_code"] == 0
    assert rep["reasons"] == []
    [snap] = history.load_snapshots(lib.cfg.state_dir)
    assert rep["run_id"] == snap.run_id                         # 同一个批次 ID
    assert (health.health_dir(lib.cfg.state_dir) / f"{rep['run_id']}.json").is_file()
    assert rep["clients"]["qbit"] == "ok"
    assert rep["torrents"] == {"current": 1, "previous": None, "previous_run": None}
    applied = [r for r in lib.audit(rep["run_id"]) if r["status"] == "applied"]
    assert rep["actions"]["applied"] == len(applied) and rep["actions"]["failed"] == 0
    assert rep["duration_s"] >= 0 and rep["started"] <= rep["finished"]
    assert "═══ 健康：✅ ok" in out and rep["run_id"] in out


def test_second_run_reports_previous_torrent_count(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    cli.cmd_run(_args(), lib.cfg)
    first = _latest(lib)["run_id"]
    cli.cmd_run(_args(), lib.cfg)
    assert _latest(lib)["torrents"] == {"current": 1, "previous": 1, "previous_run": first}


# ------------------------------------------------------------------ critical
def test_refused_run_is_critical_with_exit_3(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    lib.qbit_down()

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_DEGRADED == rep["exit_code"]
    assert rep["status"] == "critical"
    assert [r["code"] for r in rep["reasons"]] == ["refused"]
    assert rep["clients"]["qbit"] == "down"
    assert "═══ 健康：🔴 critical" in capsys.readouterr().out


def test_exception_escaping_the_run_still_writes_the_report(offline_cli, capsys, monkeypatch):
    lib = offline_cli
    _clean(lib)

    def boom(*a, **k):
        raise OSError("隔离区所在卷掉线了")

    monkeypatch.setattr(disposal, "dispose", boom)

    rc = cli.cmd_run(_args(), lib.cfg)

    out, err = capsys.readouterr()
    rep = _latest(lib)
    assert rc == cli.EXIT_CRASH == 1 == rep["exit_code"]
    assert rep["status"] == "critical" and rep["reasons"][0]["code"] == "crash"
    assert rep["crash"]["error"] == "OSError: 隔离区所在卷掉线了"
    assert "boom" in rep["crash"]["where"]
    assert "Traceback" in err                                   # 完整 traceback 进 run.err.log
    assert "═══ 健康：🔴 critical" in out


@pytest.mark.allow("audit_fallback")
def test_audit_fallback_is_critical_with_exit_4(offline_cli, capsys, monkeypatch):
    from harness import video
    from media_agent import audit
    lib = offline_cli
    s1 = lib.show("测试番").season(1)
    s1.single("[G] Test Show - 02 [1080p].mkv", size=GB, probe=video("h264"))

    def full(path, line):
        if path.name == "audit.jsonl":
            raise OSError(28, "No space left on device")
        return real(path, line)

    real = audit.append_line
    monkeypatch.setattr(audit, "append_line", full)

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_AUDIT_INCOMPLETE == rep["exit_code"]
    assert rep["status"] == "critical" and "audit_incomplete" in [r["code"] for r in rep["reasons"]]
    assert rep["actions"]["audit_problems"] >= 1



def test_disk_still_short_after_disposal_is_critical_with_exit_5(offline_cli, capsys, monkeypatch):
    lib = offline_cli
    _clean(lib)
    lib.configure(min_free_gb=50)
    monkeypatch.setattr(disposal, "free_bytes", lambda path: 10 * 10**9)   # 只剩 10 GB

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_CRITICAL == 5 == rep["exit_code"]
    assert [r["code"] for r in rep["reasons"]] == ["disk_full"]
    assert rep["trash"]["free_bytes"] == 10 * 10**9 and rep["trash"]["space_short"]


def test_exit_code_precedence():
    """几种 critical 同时出现时取靠前的：异常 1 > 拒绝 3 > 审计 4 > 其余 critical 5。"""
    assert health.exit_code_for({"crash", "refused", "disk_full"}, 0) == 1
    assert health.exit_code_for({"refused", "audit_incomplete"}, 3) == 3
    assert health.exit_code_for({"audit_incomplete", "disk_full"}, 4) == 4
    assert health.exit_code_for({"disk_full"}, 0) == 5
    assert health.exit_code_for(set(), 0) == 0


# ------------------------------------------------------------------ warn
class _Boom:
    id = "boom-rule"
    kind = "x"

    def detect(self, ctx, state):
        raise RuntimeError("database is locked")
        yield


@pytest.mark.allow("detector_error", match="boom-rule")
def test_detector_crash_is_a_warning_naming_rule_and_place(offline_cli, capsys, monkeypatch):
    lib = offline_cli
    _clean(lib)
    real = cli.build_registry

    def reg():
        r = real()
        r.register(_Boom())
        return r

    monkeypatch.setattr(cli, "build_registry", reg)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = _latest(lib)
    assert rep["status"] == "warn"
    [err] = rep["detectors"]["errors"]
    assert err["rule"] == "boom-rule" and "database is locked" in err["error"]
    assert "test_health_report.py" in err["where"]
    assert "detector_crash" in [r["code"] for r in rep["reasons"]]


@pytest.mark.allow("failed_record")
def test_failed_action_is_a_warning(offline_cli, capsys, monkeypatch):
    lib = offline_cli
    _clean(lib)
    bad = Finding(rule="r", kind="k", severity="important", summary="改一个不在库里的文件", show="测试番",
                  action=Action(op="trash", args={"path": "/definitely/not/in/library.mkv"}))

    class _One:
        id = "one"

        def detect(self, ctx, state):
            yield bad

    monkeypatch.setattr(cli, "build_registry", lambda: Registry(detectors=[_One()]))

    assert cli.cmd_run(_args(), lib.cfg) == 0
    rep = _latest(lib)
    assert rep["status"] == "warn" and rep["actions"]["failed"] == 1
    assert rep["actions"]["failed_detail"][0]["op"] == "trash"
    assert "failed_actions" in [r["code"] for r in rep["reasons"]]


def test_autobangumi_container_left_stopped_after_a_db_write_is_a_warning(offline_cli, capsys,
                                                                         monkeypatch):
    """改 AB 数据库是 docker stop → 改库 → docker start。start 失败时库已改好：记 applied 并写明"容器可能还停着"
    ——可以前只剩一行日志（不改状态）和审计里的一个字段：这一轮 ok、不发信，最早要 6 小时后下一轮 AB 登录失败才
    看得见（2026-09-26 复审）。AutoBangumi 停着，订阅就不走了。"""
    lib = offline_cli
    _clean(lib)
    lib.bangumi(id=7, official_title="测试番", title_raw="Test Show",
                save_path=str(lib.media_root / "测试番" / "Season 1"))
    fix = Finding(rule="title-match-broken", kind="title_match_broken", severity="important",
                  summary="订阅失效", show="测试番",
                  action=Action(op="fix_title_aliases", args={"bangumi_id": 7, "aliases": ["Test"]}))

    class _One:
        id = "one"

        def detect(self, ctx, state):
            yield fix

    monkeypatch.setattr(cli, "build_registry", lambda: Registry(detectors=[_One()]))
    lib.docker_fail("start")

    assert cli.cmd_run(_args(), lib.cfg) == 0                      # warn：退出码仍是 0
    rep = _latest(lib)
    assert rep["actions"]["applied"] == 1 and rep["status"] == "warn"
    [r] = [r for r in rep["reasons"] if r["code"] == "ab_container_maybe_stopped"]
    assert r["level"] == "warn" and "docker" in r["text"]


def test_unrenamed_release_names_older_than_the_threshold_warn(offline_cli, capsys):
    """发布名躺了 24 小时还没改（这里是预演，改名没执行）——2026-09-03「最新三集刮削失败」就是这种形态。"""
    lib = offline_cli
    s1 = lib.show("测试番").season(1)
    old = s1.single("[G] Test Show - 03 [1080p].mkv", size=GB, added_hours_ago=24)
    s1.single("[G] Test Show - 04 [1080p].mkv", size=GB, added_hours_ago=1)

    assert cli.cmd_run(_args(dry_run=True), lib.cfg) == 0

    rep = _latest(lib)
    assert rep["unrenamed"]["threshold_hours"] == 12
    assert [u["path"] for u in rep["unrenamed"]["old"]] == [str(old.path)]
    assert 23.5 < rep["unrenamed"]["old"][0]["hours"] < 24.5
    assert "unrenamed_old" in [r["code"] for r in rep["reasons"]]


def test_renamed_in_this_run_does_not_count_as_unrenamed(offline_cli, capsys):
    lib = offline_cli
    s1 = lib.show("测试番").season(1)
    s1.single("[G] Test Show - 03 [1080p].mkv", size=GB, added_hours_ago=24)

    assert cli.cmd_run(_args(), lib.cfg) == 0
    assert _latest(lib)["unrenamed"]["old"] == []
    assert _latest(lib)["status"] == "ok"


def test_open_stuck_findings_warn_and_acked_ones_do_not(offline_cli, capsys):
    from harness import video
    lib = offline_cli
    chi = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("尼古喵喵 S01E08.mkv", size=GB, tags="ma:S01E08", probe=chi,
              name="[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
    s1.single("[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv", size=GB + 5,
              tags="ma:S01E08", probe=chi)
    for _ in range(4):
        cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rep["status"] == "warn" and "stuck" in [r["code"] for r in rep["reasons"]]
    [s] = rep["stuck"]["open"]
    assert s["kind"] == "seal_conflict" and s["runs"] == 4

    cli.cmd_ack(argparse.Namespace(fingerprint=s["fp"], reason="等人挑", until=None,
                                   remove=False, force=False, list=False), lib.cfg)
    cli.cmd_run(_args(), lib.cfg)
    rep = _latest(lib)
    assert rep["status"] == "ok" and rep["stuck"] == {"open": [], "acked": 1}


def test_tmdb_not_configured_is_a_warning(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    lib.tmdb.enabled = False

    assert cli.cmd_run(_args(), lib.cfg) == 0
    rep = _latest(lib)
    assert rep["status"] == "warn" and [r["code"] for r in rep["reasons"]] == ["tmdb_off"]
    assert rep["clients"]["tmdb"] == "off"


# ------------------------------------------------------------------ 抓取统计
def test_grab_stats_count_proposals_409_and_metadata_timeouts():
    grab = lambda **a: Finding(rule="episode-available", kind="episode_grabbable",  # noqa: E731
                               severity="important", summary="", show="s",
                               action=Action(op="grab_episode", args=a))
    findings = [grab(episode=1), grab(episode=2), grab(episode=3), grab(episode=4),
                Finding(rule="x", kind="y", severity="minor", summary="", show="s")]

    class R:
        applied = [{"op": "grab_episode", "already_present": False,
                    "metadata": {"outcome": "ready"}},
                   {"op": "grab_episode", "already_present": True,
                    "metadata": {"outcome": "timeout"}},
                   {"op": "rename"}]
        failed = [{"op": "grab_episode"}]
        unknown = []
        skipped = [{"op": "grab_episode", "reason": "dry-run"}]

    assert health.grab_stats(findings, R) == {
        "proposed": 4, "applied": 2, "already_present": 1, "metadata_timeouts": 1,
        "failed": 1, "unknown": 0, "skipped": 1}


# ------------------------------------------------------------------ 隔离区
def test_trash_usage_and_disposal_are_in_the_report(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    junk = lib.cfg.trash_dir / "2026-09-20" / "测试番" / "Season 1" / "x.mkv"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"x" * 1000)

    cli.cmd_run(_args(), lib.cfg)

    t = _latest(lib)["trash"]
    assert t["bytes"] == 1000 and t["files"] == 1
    assert t["min_free_bytes"] == int(lib.cfg.min_free_gb * 1e9)
    assert t["disposal"]["deleted"] == 0 and t["disposal"]["refused"] == ""


# ------------------------------------------------------------------ `media-agent health`
def test_health_command_prints_latest_given_and_json(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    cli.cmd_run(_args(), lib.cfg)
    first = _latest(lib)["run_id"]
    lib.qbit_down()
    cli.cmd_run(_args(), lib.cfg)
    capsys.readouterr()

    assert cli.cmd_health(_args(), lib.cfg) == 0
    assert "critical" in capsys.readouterr().out

    assert cli.cmd_health(_args(run=first), lib.cfg) == 0
    out = capsys.readouterr().out
    assert "ok" in out and first in out

    assert cli.cmd_health(_args(run=first, json=True), lib.cfg) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == first

    assert cli.cmd_health(_args(run="nope"), lib.cfg) == 1


def test_health_command_without_reports(offline_cli, capsys):
    assert cli.cmd_health(_args(), offline_cli.cfg) == 1
    assert "还没有" in capsys.readouterr().out


def test_unwritable_health_dir_does_not_change_the_exit_code(offline_cli, capsys):
    lib = offline_cli
    _clean(lib)
    lib.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    health.health_dir(lib.cfg.state_dir).write_text("不是目录")

    assert cli.cmd_run(_args(), lib.cfg) == 0
    assert "健康报告" in capsys.readouterr().err


def test_reports_keep_the_last_60(lib):
    for i in range(63):
        health.write_report(lib.cfg.state_dir, {"run_id": f"20260920T{i:06d}.000-1", "status": "ok"})
    d = health.health_dir(lib.cfg.state_dir)
    names = sorted(p.name for p in d.iterdir())
    assert len(names) == 60 and names[0] == "20260920T000003.000-1.json"


def test_unrenamed_alert_hours_config(monkeypatch):
    assert load_config().unrenamed_alert_hours == 12.0
    monkeypatch.setenv("UNRENAMED_ALERT_HOURS", "6")
    assert load_config().unrenamed_alert_hours == 6.0
    monkeypatch.setenv("UNRENAMED_ALERT_HOURS", "-1")
    with pytest.raises(ValueError, match="UNRENAMED_ALERT_HOURS"):
        load_config()


@pytest.mark.allow("failed_record")
def test_failures_repeating_across_runs_are_called_out(offline_cli, capsys, monkeypatch):
    """2026-09-16 … 09-26 的抓取：每轮抓的是不同的集（指纹各不相同，卡住检测连不起来），错误却是同一个
    NameError。`find_failure_patterns`（以前只在 evolve 里打印）按"同一个规则、动作、错误在几个批次里出现"数，
    健康报告把"这一轮的失败里哪些是老毛病"写出来。"""
    lib = offline_cli
    _clean(lib)
    n = {"i": 0}

    class _Grab:
        id = "episode-available"

        def detect(self, ctx, state):
            n["i"] += 1
            yield Finding(rule=self.id, kind="episode_grabbable", severity="important",
                          summary=f"S01E{n['i']:02d} 可抓取", show="测试番", subject=f"S01E{n['i']:02d}",
                          action=Action(op="trash", args={"path": f"/not/in/library/{n['i']}.mkv"}))

    monkeypatch.setattr(cli, "build_registry", lambda: Registry(detectors=[_Grab()]))

    cli.cmd_run(_args(), lib.cfg)
    assert _latest(lib)["actions"]["repeated"] == []                 # 第一次：还不是"反复"
    cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    [r] = rep["actions"]["repeated"]
    assert r["rule"] == "episode-available" and r["op"] == "trash" and r["runs"] == 2
    [reason] = [x for x in rep["reasons"] if x["code"] == "failed_actions"]
    assert "2 个批次" in reason["text"]
