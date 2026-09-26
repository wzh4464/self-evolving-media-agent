"""通知邮件（`media_agent/notify.py`）：只在**变化**时发，一轮最多一封，永远不带密钥，发不出去不拦这一轮。

为什么：健康报告写在生产机的 `state/health/` 里，没人去看就等于没写。可每轮都发一封，第三天就没人看邮件了——
所以只发变化：状态变坏（ok → warn / critical、warn → critical）与从 critical 恢复；新出现的卡住问题；新进入整批拒绝；
审计开始转写到备用文件。去重状态在 `state/notify.json`。

不联网：`smtplib.SMTP_SSL` 换成替身。
"""
from __future__ import annotations

import argparse
import json

import pytest

from media_agent import cli, notify
from media_agent.config import load_config

GB = 600_000_000


class FakeSMTP:
    """`smtplib.SMTP_SSL` 的替身：记下每封信；`fail` 非空就在 send_message 时抛它。"""
    sent: list = []
    logins: list = []
    fail: Exception | None = None
    opened: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSMTP.opened.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.opened, FakeSMTP.fail = [], [], [], None
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True
    lib.configure(notify_email_to="you@example.com", notify_smtp_host="smtp.example.com",
                  notify_smtp_port=465, notify_smtp_user="agent@example.com",
                  notify_smtp_pass="app-password-xyz")
    s1 = lib.show("测试番").season(1)
    s1.single("测试番 S01E01.mkv", size=GB, name="[G] Test Show - 01 [1080p].mkv")
    return lib


def _run(lib):
    return cli.cmd_run(argparse.Namespace(dry_run=False, no_tmdb=True, no_evolve=False,
                                          max_proposals=0), lib.cfg)


def _text(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def _html(msg) -> str:
    return msg.get_body(preferencelist=("html",)).get_content()


def _state(lib) -> dict:
    return json.loads((lib.cfg.state_dir / "notify.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 只在变化时
def test_a_healthy_first_run_sends_nothing(offline_cli, smtp, capsys):
    lib = offline_cli
    assert _run(lib) == 0
    assert smtp.sent == []
    assert _state(lib)["last_status"] == "ok"


def test_ok_to_critical_sends_one_mail_and_repeats_do_not(offline_cli, smtp, capsys):
    lib = offline_cli
    _run(lib)
    lib.qbit_down()

    assert _run(lib) == cli.EXIT_DEGRADED
    assert _run(lib) == cli.EXIT_DEGRADED                       # 还是拒绝：不再发

    [msg] = smtp.sent
    assert msg["Subject"].startswith("[media-agent]") and "critical" in msg["Subject"]
    assert msg["To"] == "you@example.com" and msg["From"].endswith("<agent@example.com>")
    body = _text(msg)
    assert "ok → critical" in body and "整批拒绝改动" in body and "qBittorrent" in body
    assert "<" in _html(msg) and "整批拒绝改动" in _html(msg)
    assert smtp.logins == [("agent@example.com", "app-password-xyz")]
    assert smtp.opened == [("smtp.example.com", 465)]


def test_recovery_from_critical_sends_a_mail(offline_cli, smtp, capsys):
    lib = offline_cli
    lib.qbit_down()
    _run(lib)
    lib.qbit_up = True

    assert _run(lib) == 0

    assert len(smtp.sent) == 2
    assert "恢复" in smtp.sent[1]["Subject"] and "critical → ok" in _text(smtp.sent[1])


def test_warn_to_ok_is_not_worth_a_mail(offline_cli, smtp, capsys):
    lib = offline_cli
    lib.tmdb.enabled = False                                      # warn：TMDB 没配
    _run(lib)
    lib.tmdb.enabled = True
    _run(lib)
    assert [m["Subject"] for m in smtp.sent] == [smtp.sent[0]["Subject"]]
    assert "warn" in smtp.sent[0]["Subject"]
    assert _state(lib)["last_status"] == "ok"


def _seal_conflict(lib):
    from harness import video
    chi = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("尼古喵喵 S01E08.mkv", size=GB, tags="ma:S01E08", probe=chi,
              name="[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
    s1.single("[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv", size=GB + 5,
              tags="ma:S01E08", probe=chi)


def test_new_stuck_fingerprint_is_mailed_once(offline_cli, smtp, capsys):
    lib = offline_cli
    _seal_conflict(lib)
    for _ in range(6):
        _run(lib)

    [msg] = smtp.sent                                             # 第 4 轮：ok → warn + 新卡住，同一封
    body = _text(msg)
    assert "ok → warn" in body and "新卡住" in body and "seal_conflict" in body
    fp = [k for k in _state(lib)["active"] if k.startswith("stuck:")]
    assert len(fp) == 1 and fp[0].split(":", 1)[1] in body


def test_a_second_new_stuck_fingerprint_is_mailed_even_while_already_warn(offline_cli, smtp,
                                                                          capsys):
    lib = offline_cli
    _seal_conflict(lib)
    for _ in range(4):
        _run(lib)
    assert len(smtp.sent) == 1
    from harness import video
    chi = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
    s = lib.show("另一部").season(1)
    s.single("另一部 S01E03.mkv", size=GB, tags="ma:S01E03", probe=chi, name="[A] Other - 03.mkv")
    s.single("[B] Other - 03 [简繁内封].mkv", size=GB + 1, tags="ma:S01E03", probe=chi)
    for _ in range(4):
        _run(lib)

    assert len(smtp.sent) == 2                                    # 状态一直是 warn，照样发
    assert "新卡住" in _text(smtp.sent[1]) and "另一部" in _text(smtp.sent[1])


# ------------------------------------------------------------------ 密钥、失败、未配置
def test_secrets_never_reach_the_mail(offline_cli, smtp, capsys, monkeypatch):
    lib = offline_cli
    lib.configure(qbit_pass="hunter2-qbit", tmdb_api_key="tmdbkey123456", llm_key="sk-llm-9999")
    lib.qbit_down()
    from media_agent import health

    real = health.RunHealth.clients

    def leaky(self, ctx):
        real(self, ctx)
        self.data["clients"]["qbit"] = ("down: ConnectError: http://u:hunter2-qbit@qbit.invalid/ "
                                        "api_key=tmdbkey123456&x=1 Bearer sk-llm-9999")

    monkeypatch.setattr(health.RunHealth, "clients", leaky)

    _run(lib)

    [msg] = smtp.sent
    raw = msg.as_string()
    for secret in ("hunter2-qbit", "tmdbkey123456", "sk-llm-9999", "app-password-xyz"):
        assert secret not in raw
    rep = json.loads(next((lib.cfg.state_dir / "health").glob("2*.json")).read_text())
    assert "hunter2-qbit" not in json.dumps(rep, ensure_ascii=False)


def test_send_failure_is_logged_counted_and_retried(offline_cli, smtp, capsys):
    lib = offline_cli
    _run(lib)
    lib.qbit_down()
    smtp.fail = OSError("Connection refused")

    assert _run(lib) == cli.EXIT_DEGRADED                         # 退出码不因通知而变
    err = capsys.readouterr().err
    assert "通知邮件没发出去" in err and "Connection refused" in err
    st = _state(lib)
    assert st["failures"] == 1                                    # 没通知到：事件留着，下一封补上
    assert [e["kind"] for e in st["undelivered"]] == ["status", "degraded"]

    smtp.fail = None
    _run(lib)
    [msg] = smtp.sent
    assert "ok → critical" in _text(msg)
    assert _state(lib)["failures"] == 0


def test_unconfigured_means_disabled_and_silent(lib, smtp, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.qbit_down()
    _run(lib)
    assert smtp.opened == [] and not (lib.cfg.state_dir / "notify.json").exists()
    assert "通知" not in capsys.readouterr().err


def test_half_configured_is_disabled_with_a_warning(lib, smtp, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.configure(notify_email_to="you@example.com")              # 没有 NOTIFY_SMTP_HOST
    lib.qbit_down()
    _run(lib)
    assert smtp.opened == []
    assert "NOTIFY_SMTP_HOST" in capsys.readouterr().err


def test_config_keys_and_defaults(monkeypatch):
    cfg = load_config()
    assert (cfg.notify_email_to, cfg.notify_smtp_host, cfg.notify_smtp_port,
            cfg.notify_smtp_user, cfg.notify_smtp_pass) == ("", "", 465, "", "")
    monkeypatch.setenv("NOTIFY_SMTP_PORT", "x")
    with pytest.raises(ValueError, match="NOTIFY_SMTP_PORT"):
        load_config()


def test_redact_masks_configured_secrets_and_common_url_params(lib):
    cfg = lib.configure(qbit_pass="p@ss w0rd!", tmdb_api_key="abc123")
    text = notify.redact(cfg, "x p@ss w0rd! y ?api_key=abc123&token=zzz Authorization: Bearer qqq "
                              "http://admin:abc123@host/")
    assert "p@ss w0rd!" not in text and "abc123" not in text and "zzz" not in text
    assert "qqq" not in text and "***" in text


def test_short_secrets_are_not_replaced_verbatim_everywhere(lib):
    """`test` 这种短密码按原文替换会把路径里的 `pytest` / `test_…` 也换掉（健康报告的路径就对不上了）；
    它们只会随 URL / 请求头出现，那里由模式遮掉。"""
    cfg = lib.configure(qbit_pass="test")
    path = "/tmp/pytest-1/test_x/Media/测试番/Season 1/a.mkv"
    assert notify.redact(cfg, path) == path
    assert "test@" not in notify.redact(cfg, "http://admin:test@qbit.invalid/api")


# ------------------------------------------------------------------ 没评估的事件原样带着（2026-09-26 复审）
def _stuck_report(fp="aaaaaaaaaaaaaaaa", status="warn", **extra):
    return {"status": status, "finished": "2026-09-26T12:00:00", "actions": {"audit_problems": 0},
            "degraded": {"refused": ""},
            "stuck": {"open": [{"fp": fp, "rule": "r", "kind": "k", "show": "s", "runs": 4,
                                "first_seen": "2026-09-25T00:00:00", "summary": "摘要"}],
                      "acked": 0}, **extra}


def _skipped_report(status="warn", **extra):
    """被锁挡住 / 暂停 / 半路崩溃的一轮：没走到执行器，也没走到卡住检测（`RunHealth` 里是 None）。"""
    return {"status": status, "finished": "2026-09-26T18:00:00", "actions": None,
            "degraded": {"refused": ""}, "stuck": None, **extra}


def test_a_run_that_never_evaluated_stuck_keeps_the_stuck_fingerprints():
    """以前 `active` 每轮从零重建，被锁挡住的一轮（stuck 为 None）把 `stuck:<指纹>` 全清了——下一轮正常的 run
    把每个一直卡着的指纹当「新卡住」再发一遍。部署、看门狗重建容器、救援暂停、qBit 超时都会触发。"""
    evs, st = notify.events(_stuck_report(), {"last_status": "ok", "active": {}})
    assert [e["kind"] for e in evs] == ["status", "stuck"]

    evs, st = notify.events(_skipped_report(locked="pid=1 cmd=deploy.sh"), st)
    assert evs == [] and "stuck:aaaaaaaaaaaaaaaa" in st["active"]

    evs, st = notify.events(_stuck_report(), st)
    assert evs == []


def test_stuck_that_resolves_in_an_evaluated_run_is_mailed_again_when_it_comes_back():
    """带着走只针对"没评估"的轮次：正常的一轮里指纹不在了（解决了），再出现照发（模块文档的规则）。"""
    _, st = notify.events(_stuck_report(), {"last_status": "ok", "active": {}})
    healthy = {**_stuck_report(), "stuck": {"open": [], "acked": 0}}
    _, st = notify.events(healthy, st)
    assert not any(k.startswith("stuck:") for k in st["active"])
    evs, _ = notify.events(_stuck_report(), st)
    assert [e["kind"] for e in evs] == ["stuck"]


def test_refused_then_paused_then_refused_announces_the_refusal_once():
    """暂停 / 被锁挡住的一轮没走到执行器：不知道还拒不拒绝，`degraded` 原样带着。"""
    refused = {**_skipped_report(status="critical"), "degraded": {"refused": "qBittorrent 登录超时"}}
    evs, st = notify.events(refused, {"last_status": "ok", "active": {}})
    assert [e["kind"] for e in evs] == ["status", "degraded"]
    _, st = notify.events(_skipped_report(paused="state/PAUSE"), st)
    assert "degraded" in st["active"]
    evs, st = notify.events(refused, st)
    assert "degraded" not in [e["kind"] for e in evs]


def test_audit_fallback_is_carried_through_a_run_that_wrote_no_audit():
    bad = {**_stuck_report(), "stuck": {"open": [], "acked": 0}, "actions": {"audit_problems": 2}}
    evs, st = notify.events(bad, {"last_status": "warn", "active": {}})
    assert [e["kind"] for e in evs] == ["audit_fallback"]
    _, st = notify.events(_skipped_report(locked="pid=1"), st)
    assert "audit_fallback" in st["active"]
    evs, _ = notify.events(bad, st)
    assert evs == []


def test_stuck_item_through_refused_paused_and_locked_runs_is_mailed_once(offline_cli, smtp,
                                                                          capsys):
    """端到端：卡住之后 qBit 掉线（整批拒绝）、维护暂停、被锁挡住，再回到正常——同一个指纹只报一次「新卡住」。"""
    lib = offline_cli
    _seal_conflict(lib)
    for _ in range(4):
        _run(lib)                                                 # 第 4 轮：新卡住
    lib.qbit_down()
    assert _run(lib) == cli.EXIT_DEGRADED                         # critical：整批拒绝
    lib.qbit_up = True
    args = argparse.Namespace(run_id="20260926T180000.000-1", dry_run=False)
    assert cli._paused_run(args, lib.cfg, "state/PAUSE（测试）") == cli.EXIT_LOCKED
    args = argparse.Namespace(run_id="20260926T190000.000-1", dry_run=False)
    assert cli._locked_out_run(args, lib.cfg, "pid=1 cmd=vpn-watchdog.sh") == cli.EXIT_LOCKED
    _run(lib)
    _run(lib)

    bodies = [_text(m) for m in smtp.sent]
    assert sum("新卡住" in b for b in bodies) == 1, bodies


# ------------------------------------------------------------------ 发不出去的事件留着补发（2026-09-26 复审）
def test_a_transient_critical_whose_mail_failed_is_delivered_with_the_next_mail(lib, monkeypatch):
    """以前发不出去时只是不推进 `last_status`，指望下一轮重算——可下一轮若已经回到 ok，重算什么都没有，
    这次整批拒绝就谁也不知道了（只剩生产机上那份健康报告）。现在没发出去的事件留在 notify.json 里，
    下一封信补上（写明是哪一批的）。"""
    cfg = lib.configure(notify_email_to="you@example.com", notify_smtp_host="smtp.example.com")
    sent, calls = [], []

    def send(cfg_, msg):
        calls.append(msg)
        if len(calls) == 1:
            raise OSError("smtp down")
        sent.append(msg)

    monkeypatch.setattr(notify, "send", send)
    acts = {"applied": 0, "skipped": 0, "failed": 0, "unknown": 0, "audit_problems": 0}
    ok = {"run_id": "r0", "status": "ok", "finished": "2026-09-26T00:00:00", "reasons": [],
          "actions": acts, "degraded": {"refused": ""}, "stuck": {"open": [], "acked": 0}}
    assert notify.maybe_send(cfg, ok)["sent"] is False               # 第一轮 ok：没什么要说
    critical = {**ok, "run_id": "r1", "status": "critical", "finished": "2026-09-26T06:00:00",
                "actions": None, "degraded": {"refused": "qBittorrent 登录超时"},
                "reasons": [{"level": "critical", "code": "refused", "text": "整批拒绝改动"}]}
    res = notify.maybe_send(cfg, critical)
    assert res["sent"] is False and "smtp down" in res["error"]

    res = notify.maybe_send(cfg, {**ok, "run_id": "r2", "finished": "2026-09-26T12:00:00"})

    assert res["sent"] is True and len(calls) == 2
    body = _text(sent[0])
    assert "ok → critical" in body and "新进入整批拒绝" in body and "r1" in body and "补发" in body
    assert "critical → ok" in body                                   # 这一轮自己的：从 critical 恢复
    st = _state(lib)
    assert st["failures"] == 0 and not st.get("undelivered")


def test_undelivered_events_are_capped_and_the_rest_counted(lib, monkeypatch):
    """SMTP 一直配错时 notify.json 不能无限长：只留最近 50 条，更早的计数，补发时说一句。"""
    cfg = lib.configure(notify_email_to="you@example.com", notify_smtp_host="smtp.example.com")
    st = {"last_status": "warn", "active": {}, "failures": 9,
          "undelivered": [{"kind": "stuck", "text": f"新卡住 {i}", "run_id": f"r{i}", "at": "t"}
                          for i in range(50)]}
    (cfg.state_dir / "notify.json").write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setattr(notify, "send", lambda c, m: (_ for _ in ()).throw(OSError("still down")))
    rep = {"run_id": "r50", "status": "critical", "finished": "t50", "actions": None, "stuck": None,
           "degraded": {"refused": "x"}, "reasons": []}
    notify.maybe_send(cfg, rep)
    after = _state(lib)
    assert len(after["undelivered"]) == 50 and after["undelivered_dropped"] == 2
    assert after["undelivered"][-1]["run_id"] == "r50"

    sent = []
    monkeypatch.setattr(notify, "send", lambda c, m: sent.append(m))
    notify.maybe_send(cfg, {**rep, "run_id": "r51"})
    body = _text(sent[0])
    assert "另有 2 条更早的事件" in body and "批次 r49，" in body
    assert "批次 r0，" not in body and "批次 r1，" not in body


# ------------------------------------------------------------------ 已经 critical 时的「新进入」事件
def _critical(**extra):
    return {"status": "critical", "finished": "2026-09-26T12:00:00", "stuck": {"open": [], "acked": 0},
            "degraded": {"refused": ""}, "actions": {"audit_problems": 0}, **extra}


def test_first_refusal_is_mailed_even_when_already_critical_and_only_once():
    """已经因为磁盘满 critical（退出码 5）时，状态没变——「新进入整批拒绝」是这一封信唯一的理由。"""
    st = {"last_status": "critical", "active": {}}
    refused = _critical(actions=None, degraded={"refused": "种子数骤降"})
    evs, st = notify.events(refused, st)
    assert [(e["kind"], e["text"]) for e in evs] == [("degraded", "新进入整批拒绝：种子数骤降")]
    evs, st = notify.events(refused, st)
    assert evs == []
    evs, st = notify.events(_critical(), st)                      # 执行器跑完、没拒绝：结束
    assert evs == [] and "degraded" not in st["active"]
    evs, _ = notify.events(refused, st)
    assert [e["kind"] for e in evs] == ["degraded"]               # 再进入，再发


def test_first_audit_fallback_is_mailed_even_when_already_critical_and_only_once():
    st = {"last_status": "critical", "active": {}}
    bad = _critical(actions={"audit_problems": 3})
    evs, st = notify.events(bad, st)
    assert [e["kind"] for e in evs] == ["audit_fallback"] and "3 条审计" in evs[0]["text"]
    evs, st = notify.events(bad, st)
    assert evs == []
    evs, st = notify.events(_critical(), st)
    assert "audit_fallback" not in st["active"]
    evs, _ = notify.events(bad, st)
    assert [e["kind"] for e in evs] == ["audit_fallback"]
