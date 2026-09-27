"""`AB_MODE` 与 `media-agent ab-mode [show|subscription|full] [--dry-run]`：可逆地关掉 / 打开 AutoBangumi 的下载与改名。

用户**通过 AB 订阅**（critic §4），AB 不能整个关掉：只关它的两个后台线程（`rss_parser.enable`：拉 RSS、下载；
`bangumi_manage.enable`：改名），WebUI 与订阅照旧（ab 调研 §3.1 / §3.2）。两个开关只在 AB 程序重启时生效，改配置要发回
**整份**对象（漏掉的段 AB 会退回默认值：`downloader.host` 变成 `172.17.0.1:8080`）。

- 本项目认的模式：`state/ab_mode.json`（命令切换成功时写）> `.env` 的 `AB_MODE` > `full`；写错了、状态文件坏了大声失败。
- 切换经执行器（动作 `set_ab_mode`）：审计、逆操作（原来的开关与原来的状态文件）、`rollback` 能退；接口连不上拒绝
  （只读 config.json 说一句看到了什么）；重启之后等不到 AB 回来记 unknown、说清怎么退；读回的开关对上了才记 applied、
  才写状态文件（带核对用的基线：每个 rssitem 的 `last_checked_at`、已有的订阅 id）。
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest

from media_agent import abmode, cli, config as config_mod, runlock
from media_agent.config import load_config
from media_agent.runlock import LOCK_NAME, RunLock

ON = {"rss_parser.enable": True, "bangumi_manage.enable": True}
OFF = {"rss_parser.enable": False, "bangumi_manage.enable": False}
T0 = "2026-09-27T03:45:00.000000+00:00"


@pytest.fixture
def ab_lib(lib, monkeypatch):
    """一个有 AB 库（两条订阅、两个 rssitem）与 AB 接口的现场；`cli` 的上下文与配置都接到它。"""
    lib.show("甲番").bangumi(1, title_raw="Jia")
    lib.show("乙番").bangumi(2, title_raw="Yi", season=2)
    lib.ab_rows("rssitem", [{"id": 1, "name": "甲番", "url": "https://mikan.invalid/RSS/Bangumi?bangumiId=1",
                             "last_checked_at": T0},
                            {"id": 2, "name": "乙番", "url": "https://mikan.invalid/RSS/Bangumi?bangumiId=2",
                             "last_checked_at": T0}])
    lib.over: dict = {}
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context(**lib.over))
    monkeypatch.setattr(cli, "load_config", lambda: lib.cfg)
    monkeypatch.setattr(abmode, "POLL_S", 0.0)
    return lib


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["media-agent", *argv])
    return cli.main()


def _state(lib):
    return abmode.read_state(lib.cfg.state_dir)


def _set_audit(lib) -> list[dict]:
    return [r for r in lib.audit() if r.get("op") == "set_ab_mode"]


# ================================================================ 配置
def test_the_default_is_full_so_deploying_changes_nothing():
    cfg = load_config()
    assert (cfg.ab_mode, cfg.ab_mode_source) == ("full", "default")


def test_ab_mode_comes_from_the_env(monkeypatch):
    monkeypatch.setenv("AB_MODE", "Subscription")
    cfg = load_config()
    assert (cfg.ab_mode, cfg.ab_mode_source) == ("subscription", "env")


def test_a_typo_in_ab_mode_fails_loudly(monkeypatch):
    """静默当成哪一种都可能错：当成 full 让停了改名的 AB 继续"拥有" Bangumi 分类，当成 subscription 抢 AB 还在改的文件。"""
    monkeypatch.setenv("AB_MODE", "subscribe")
    with pytest.raises(ValueError, match="AB_MODE"):
        load_config()


def test_the_state_file_written_by_the_command_wins_over_the_env(monkeypatch, project_root):
    monkeypatch.setenv("AB_MODE", "full")
    abmode.write_state(project_root / "state", {"mode": "subscription"})
    cfg = load_config()
    assert (cfg.ab_mode, cfg.ab_mode_source) == ("subscription", "state")


@pytest.mark.parametrize("body", ["{not json", "[]", '{"mode": "half"}'])
def test_a_broken_state_file_fails_loudly(project_root, body):
    p = project_root / "state" / abmode.STATE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="ab_mode.json"):
        load_config()


def test_ab_config_path_is_optional(monkeypatch, tmp_path):
    assert load_config().ab_config is None
    monkeypatch.setenv("AB_CONFIG", str(tmp_path / "config.json"))
    assert load_config().ab_config == tmp_path / "config.json"


# ================================================================ 切到 subscription
def test_switching_to_subscription_flips_only_the_two_flags_and_restarts(ab_lib, monkeypatch, capsys):
    lib = ab_lib
    before = json.loads(json.dumps(lib.ab.config))

    assert _main(monkeypatch, "ab-mode", "subscription") == 0

    # 只改两个开关，其余原样——整份发回（漏发的段会被 AB 退回默认值）
    after = lib.ab.config
    assert lib.ab.flags() == OFF
    for section in before:
        if section not in ("rss_parser", "bangumi_manage"):
            assert after[section] == before[section], section
    assert after["downloader"]["password"] == lib.ab.SECRET
    assert {k: v for k, v in after["rss_parser"].items() if k != "enable"} == \
           {k: v for k, v in before["rss_parser"].items() if k != "enable"}
    # 程序重启过、线程按新开关起
    assert lib.ab.calls.index("update_config") < lib.ab.calls.index("restart")
    assert lib.ab.running == {"rss": False, "renamer": False}

    rec = _state(lib)
    assert rec["mode"] == "subscription" and rec["previous_flags"] == ON
    assert rec["baseline"] == {"rss_last_checked": {"1": T0, "2": T0}, "bangumi_ids": [1, 2]}
    assert rec["switched_at_epoch"] > 0 and rec["run_id"]

    [a] = _set_audit(lib)
    assert a["status"] == "applied"
    assert a["undo"]["op"] == "set_ab_mode" and a["undo"]["flags"] == ON and a["undo"]["set"] == OFF
    assert a["undo"]["prev_state"] is None
    out = capsys.readouterr().out
    assert "subscription" in out and "media-agent ab-mode full" in out


def test_the_command_is_what_config_reads_next_time(ab_lib, monkeypatch):
    """命令写的状态文件盖过 .env：下一次 `load_config` 就是新的模式。"""
    monkeypatch.setenv("AB_MODE", "full")
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    cfg = config_mod.load_config()
    assert (cfg.ab_mode, cfg.ab_mode_source) == ("subscription", "state")


def test_a_slow_restart_is_waited_for(ab_lib, monkeypatch):
    """`Program.start()` 先等下载器：重启请求超时，之后几次 status 是 false——接着等，回来了照常核对。"""
    ab_lib.ab.slow_restart(polls=3)
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    assert _set_audit(ab_lib)[0]["status"] == "applied"
    assert _state(ab_lib)["mode"] == "subscription"


@pytest.mark.allow("unknown_record", match="set_ab_mode")
def test_a_restart_that_never_comes_back_is_unknown_with_revert_instructions(ab_lib, monkeypatch, capsys):
    lib = ab_lib
    lib.ab.hang_restart()
    monkeypatch.setattr(abmode, "RESTART_TIMEOUT_S", 0.0)

    assert _main(monkeypatch, "ab-mode", "subscription") == 1

    [a] = _set_audit(lib)
    assert a["status"] == "unknown" and a["undo"]["flags"] == ON
    assert "ab.update_config" in a["effects_attempted"]
    # 本项目的模式不动：AB 那边说不清
    assert _state(lib) is None
    out = capsys.readouterr().out
    assert "media-agent ab-mode full" in out and "ab-mode show" in out and "rollback" in out


@pytest.mark.allow("unknown_record", match="set_ab_mode")
def test_a_restart_request_that_never_reached_ab_is_unknown(ab_lib, monkeypatch):
    """请求根本没发出去：AB 没重启，config.json 里已是新开关、线程还是旧的——说不清，不当成功。"""
    ab_lib.ab.fail("restart", exc=httpx.ConnectError("refused"))
    assert _main(monkeypatch, "ab-mode", "subscription") == 1
    assert _set_audit(ab_lib)[0]["status"] == "unknown"
    assert ab_lib.ab.running == {"rss": True, "renamer": True}
    assert _state(ab_lib) is None


@pytest.mark.allow("unknown_record", match="set_ab_mode")
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("connect timed out"), httpx.PoolTimeout("pool timed out"),
                                 httpx.WriteTimeout("write timed out")], ids=lambda e: type(e).__name__)
def test_a_restart_request_that_timed_out_before_reaching_ab_is_unknown(ab_lib, monkeypatch, exc):
    """连接 / 连接池 / 写请求超时：请求多半没到 AB。PATCH 已经让 AB 重载了设置（读回的开关是新的），旧程序也还在跑
    （status 为真）——读回核对什么都证明不了。以前一律当"在重启、接着等"，记 applied、写状态文件，AB 的两个线程其实
    照旧在拉 RSS、改名（2026-09-27 审查复现）。"""
    ab_lib.ab.fail("restart", exc=exc)
    monkeypatch.setattr(abmode, "RESTART_TIMEOUT_S", 0.0)

    assert _main(monkeypatch, "ab-mode", "subscription") == 1

    [a] = _set_audit(ab_lib)
    assert a["status"] == "unknown"
    assert ab_lib.ab.running == {"rss": True, "renamer": True}
    assert _state(ab_lib) is None


@pytest.mark.allow("unknown_record", match="set_ab_mode")
def test_a_read_timeout_is_accepted_only_after_ab_was_seen_restarting(ab_lib, monkeypatch):
    """读超时 = 请求到了、AB 在等下载器。可 AB 真在重启的话 status 会先是 false：一直是真（旧程序还在跑）就没法说它
    重启过，等到时限记 unknown。"""
    ab_lib.ab.fail("restart", exc=httpx.ReadTimeout("timed out"))       # 请求报超时、AB 那边什么都没发生
    monkeypatch.setattr(abmode, "RESTART_TIMEOUT_S", 0.0)

    assert _main(monkeypatch, "ab-mode", "subscription") == 1

    assert _set_audit(ab_lib)[0]["status"] == "unknown"
    assert ab_lib.ab.running == {"rss": True, "renamer": True}
    assert _state(ab_lib) is None


@pytest.mark.allow("failed_record", match="set_ab_mode")
def test_a_failed_update_that_did_not_land_is_failed_and_does_not_restart(ab_lib, monkeypatch):
    ab_lib.ab.fail("update_config", exc=httpx.ReadTimeout("timed out"))
    assert _main(monkeypatch, "ab-mode", "subscription") == 1
    [a] = _set_audit(ab_lib)
    assert a["status"] == "failed" and "没有生效" in a["effect"]
    assert "restart" not in ab_lib.ab.calls
    assert ab_lib.ab.flags() == ON and _state(ab_lib) is None


def test_ab_unreachable_is_refused_after_a_read_only_look_at_config_json(ab_lib, monkeypatch, tmp_path, capsys):
    lib = ab_lib
    conf = tmp_path / "ab-config" / "config.json"
    conf.parent.mkdir()
    conf.write_text(json.dumps(lib.ab.config), encoding="utf-8")
    raw = conf.read_bytes()
    lib.configure(ab_config=conf)
    lib.over["ab"] = None

    assert _main(monkeypatch, "ab-mode", "subscription") == cli.EXIT_DEGRADED

    out = capsys.readouterr().out
    assert "rss_parser.enable=true" in out and "只读" in out
    assert conf.read_bytes() == raw                    # 只读
    assert not _set_audit(lib) and _state(lib) is None


def test_ab_unreachable_without_a_config_path_is_refused(ab_lib, monkeypatch, capsys):
    ab_lib.over["ab"] = None
    assert _main(monkeypatch, "ab-mode", "subscription") == cli.EXIT_DEGRADED
    assert "AB_CONFIG" in capsys.readouterr().out


def test_an_unrecognised_config_shape_is_refused(ab_lib, monkeypatch):
    del ab_lib.ab.config["bangumi_manage"]
    assert _main(monkeypatch, "ab-mode", "subscription") == cli.EXIT_DEGRADED
    assert "update_config" not in ab_lib.ab.calls


def test_dry_run_changes_nothing(ab_lib, monkeypatch, capsys):
    assert _main(monkeypatch, "ab-mode", "subscription", "--dry-run") == 0
    assert ab_lib.ab.flags() == ON
    assert "update_config" not in ab_lib.ab.calls and "restart" not in ab_lib.ab.calls
    assert _state(ab_lib) is None
    assert "预演" in capsys.readouterr().out


def test_already_in_the_mode_is_a_no_op(ab_lib, monkeypatch, capsys):
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    ab_lib.ab.calls.clear()
    ab_lib.configure(ab_mode="subscription", ab_mode_source="state")
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    assert "update_config" not in ab_lib.ab.calls and "restart" not in ab_lib.ab.calls
    assert "已经是" in capsys.readouterr().out


def test_full_on_a_fresh_deploy_is_a_no_op(ab_lib, monkeypatch, capsys):
    """刚部署（默认 full、AB 的开关都开着）再说一次 full：没有基线要记、没有东西要切——不为记一笔去重启 AB。"""
    assert _main(monkeypatch, "ab-mode", "full") == 0
    assert "restart" not in ab_lib.ab.calls and _state(ab_lib) is None
    assert "已经是" in capsys.readouterr().out


def test_flags_already_off_in_ab_still_restart_and_record(ab_lib, monkeypatch):
    """人在 WebUI 里关过（或 PATCH 之后没重启）：不再 PATCH，但照样重启一次让线程对上配置、核对、记基线。"""
    lib = ab_lib
    lib.ab.config["rss_parser"]["enable"] = lib.ab.config["bangumi_manage"]["enable"] = False
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    assert "update_config" not in lib.ab.calls and "restart" in lib.ab.calls
    assert lib.ab.running == {"rss": False, "renamer": False}
    assert _state(lib)["mode"] == "subscription"


def test_mixed_flags_are_switched_both_ways(ab_lib, monkeypatch):
    ab_lib.ab.config["bangumi_manage"]["enable"] = False
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    assert ab_lib.ab.flags() == OFF
    assert _state(ab_lib)["previous_flags"] == {"rss_parser.enable": True, "bangumi_manage.enable": False}


# ================================================================ 切回 full
def test_switching_back_to_full_turns_both_on_and_warns_about_the_catch_up(ab_lib, monkeypatch, capsys):
    lib = ab_lib
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    lib.configure(ab_mode="subscription", ab_mode_source="state")
    capsys.readouterr()

    assert _main(monkeypatch, "ab-mode", "full") == 0

    assert lib.ab.flags() == ON and lib.ab.running == {"rss": True, "renamer": True}
    rec = _state(lib)
    assert rec["mode"] == "full" and rec["previous_flags"] == OFF and rec.get("baseline") is None
    out = capsys.readouterr().out
    assert "补下载" in out                           # AB 按 URL 判新：暂停期间发布的一口气补


# ================================================================ 回退
def test_rollback_restores_the_flags_and_the_previous_state(ab_lib, monkeypatch):
    lib = ab_lib
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    run_id = _set_audit(lib)[0]["run_id"]

    res = lib.rollback(run_id)

    assert res["reverted"] == 1, res
    assert lib.ab.flags() == ON and lib.ab.running == {"rss": True, "renamer": True}
    assert _state(lib) is None                        # 切之前没有状态文件：回退后也没有


def test_rollback_does_not_touch_flags_a_human_changed_since(ab_lib, monkeypatch):
    lib = ab_lib
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    run_id = _set_audit(lib)[0]["run_id"]
    lib.ab.config["rss_parser"]["enable"] = True        # 人在 WebUI 里又开了一个
    lib.ab.calls.clear()

    res = lib.rollback(run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "有人改过" in res["skipped_detail"][0]["skip_reason"]
    assert "update_config" not in lib.ab.calls
    assert _state(lib)["mode"] == "subscription"


def test_rollback_dry_run_changes_nothing(ab_lib, monkeypatch):
    lib = ab_lib
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    run_id = _set_audit(lib)[0]["run_id"]
    lib.ab.calls.clear()
    res = lib.rollback(run_id, dry_run=True)
    assert res["reverted"] == 1
    assert lib.ab.flags() == OFF and "update_config" not in lib.ab.calls
    assert _state(lib)["mode"] == "subscription"


# ================================================================ show
def test_show_reports_both_sides_and_agreement(ab_lib, monkeypatch, capsys):
    assert _main(monkeypatch, "ab-mode") == 0
    out = capsys.readouterr().out
    assert "full" in out and "default" in out and "rss_parser.enable=true" in out and "一致" in out


def test_show_says_when_the_sides_disagree(ab_lib, monkeypatch, capsys):
    """本项目以为 AB 停了，AB 其实还开着：两边都在"拥有" Bangumi 分类 / 都不管——退出码 1，说出来。"""
    ab_lib.configure(ab_mode="subscription", ab_mode_source="env")
    assert _main(monkeypatch, "ab-mode", "show") == 1
    out = capsys.readouterr().out
    assert "不一致" in out and "media-agent ab-mode subscription" in out


def test_show_reports_polling_after_the_switch(ab_lib, monkeypatch, capsys):
    lib = ab_lib
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    lib.configure(ab_mode="subscription", ab_mode_source="state")
    lib.ab.running["rss"] = True                         # 线程其实没停（重启没生效）
    lib.ab.poll_rss("2026-09-27T09:00:00+00:00")
    capsys.readouterr()

    assert _main(monkeypatch, "ab-mode", "show") == 1
    assert "拉过 RSS" in capsys.readouterr().out


def test_the_switch_takes_the_run_lock(ab_lib, monkeypatch):
    monkeypatch.setattr(runlock, "DEFAULT_WAIT", 0.1)
    held = RunLock(ab_lib.cfg.state_dir / LOCK_NAME, label="media-agent run")
    assert held.acquire(wait=0)
    try:
        assert _main(monkeypatch, "ab-mode", "subscription") == cli.EXIT_LOCKED
        assert "update_config" not in ab_lib.ab.calls
        # show 只读，不拿锁
        assert _main(monkeypatch, "ab-mode", "show") == 0
    finally:
        held.release()


# ================================================================ 之后的核对（启发式，`abmode.activity`）
def _act(tmp_path, *, torrents=(), rss_rows=({"id": 1, "name": "甲番", "last_checked_at": T0},), bangumi_rows=(),
         switched=1000.0):
    media = tmp_path / "Media"
    rec = {"mode": "subscription", "since": "2026-09-27T12:00:00", "switched_at_epoch": switched,
           "baseline": {"rss_last_checked": {"1": T0}, "bangumi_ids": [1, 2]}}
    return abmode.activity(rec, rss_rows=list(rss_rows), bangumi_rows=list(bangumi_rows),
                           torrents=list(torrents), media_root=media), media


def _t(h, media, rel, *, added, category="Bangumi", tags=""):
    return {"hash": h * 8, "name": f"[G] {h}", "save_path": str(media / rel), "category": category,
            "tags": tags, "added_on": added}


def test_a_new_subscriptions_first_burst_is_expected_and_later_adds_are_not(tmp_path):
    """订阅那一刻 AB 把已发布的集一口气加进来（新订阅那一刻还没有 id，种子不带 `ab:` 标签）：在新订阅的保存路径下、
    这一批之内的算"订阅带来的"；同一路径一小时之后又冒出来的、老订阅带 `ab:` 标签的，都是订阅动作之外的。"""
    media = tmp_path / "Media"
    new_sub = {"id": 3, "save_path": "/app/Media/丙番/Season 1"}        # 容器里的挂载点与宿主不同也认
    ts = [_t("a", media, "丙番/Season 1", added=2000), _t("b", media, "丙番/Season 1", added=2030),
          _t("c", media, "丙番/Season 1", added=2000 + abmode.SUBSCRIBE_WINDOW_S + 60),
          _t("d", media, "甲番/Season 1", added=2100, category="甲番", tags="ab:1"),
          _t("e", media, "甲番/Season 1", added=2200, category="甲番", tags="ab:1,ma:S01E05"),   # 本项目抓的
          _t("f", media, "甲番/Season 1", added=900, tags="ab:1")]                              # 切换之前
    act, _ = _act(tmp_path, torrents=ts, bangumi_rows=[{"id": 1, "save_path": "/app/Media/甲番/Season 1"}, new_sub])
    assert [x["hash"][0] for x in act["adds"]["subscribe"]] == ["a", "b"]
    assert [x["hash"][0] for x in act["adds"]["outside"]] == ["d", "c"]
    assert [p["code"] for p in abmode.activity_problems(act)] == ["ab_added_outside_subscribe"]


def test_polling_is_any_change_of_last_checked_at(tmp_path):
    act, _ = _act(tmp_path, rss_rows=[{"id": 1, "name": "甲番", "last_checked_at": T0},
                                      {"id": 5, "name": "新订阅", "last_checked_at": None}])
    assert act["polled"] == []
    act, _ = _act(tmp_path, rss_rows=[{"id": 1, "name": "甲番", "last_checked_at": "2026-09-27T09:00:00+00:00"},
                                      {"id": 5, "name": "新订阅", "last_checked_at": "2026-09-27T09:00:00+00:00"}])
    assert [p["id"] for p in act["polled"]] == [1, 5]
    assert "拉过 RSS" in abmode.activity_problems(act)[0]["text"]


def test_without_a_baseline_nothing_can_be_verified(tmp_path):
    act = abmode.activity(None, rss_rows=[], bangumi_rows=[], torrents=[], media_root=tmp_path)
    assert act["baseline"] is False
    [p] = abmode.activity_problems(act)
    assert "没有切换基线" in p["text"] and "AB_MODE" in p["text"]
    # 经命令切过、只是切换时 AB 库读不了：说的是这个，不是"模式来自 AB_MODE"
    act = abmode.activity({"mode": "subscription", "baseline": None}, rss_rows=[], bangumi_rows=[], torrents=[],
                          media_root=tmp_path)
    [p] = abmode.activity_problems(act)
    assert "AB 库读不了" in p["text"] and "AB_MODE、" not in p["text"]


def test_a_switch_without_an_ab_db_records_no_baseline_and_says_so(ab_lib, monkeypatch, capsys):
    ab_lib.over["abdb"] = None
    assert _main(monkeypatch, "ab-mode", "subscription") == 0
    assert _state(ab_lib)["baseline"] is None
    assert "没记下" in capsys.readouterr().out
