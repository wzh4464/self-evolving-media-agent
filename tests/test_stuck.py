"""卡住检测：同一个问题（同一个指纹）连续 STUCK_RUNS 轮都在，升级成"卡住"，写明从哪一轮起、连着几轮。

为什么：「集位被占」每轮一条跳过、连跳 28 轮（runloop §8a）；抓取 12 次 NameError 连着十天——每一轮单独看都
只是一条 skipped / failed，放在一起看才知道在原地打转。

只看**有动作**的、或严重度 ≥ important 的发现：`incomplete_season`「等发布即可」这种 minor 的挂多久都正常。
只数 `run` 的快照（`diagnose` 手动跑几次不能让它提前升级），读 qBittorrent 不完整的一轮不算数也不打断。

确认（`.agents/acks.json`，`media-agent ack`）：已知要人处理、短期不会动的（胆大党 / 100 个女朋友的
`season_layout_mismatch` 要人重排目录）静音；`until` 过了就重新提醒。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

import pytest

from media_agent import cli, history
from media_agent.config import load_config
from media_agent.kernel import Action, Finding


def _layout(show="胆大党", summary="库内有 Season [2] 而 TMDB 只有 Season [1]"):
    return Finding(rule="episode-available", kind="season_layout_mismatch",
                   severity="important", summary=summary, show=show, classified=True)


def _minor():
    return Finding(rule="incomplete-season", kind="incomplete_season", severity="minor",
                   summary="S04 缺 1 集（均在 7 天内播出，等发布即可）", show="入间同学入魔了！",
                   subject="S04")


def _rename(path="/m/a.mkv"):
    return Finding(rule="unrenamed-file", kind="unrenamed", severity="minor", summary="a → b",
                   show="x", path=path,
                   action=Action(op="rename", args={"path": path, "new_name": "b.mkv"}))


T0 = datetime(2026, 9, 20, 0, 0, 0)


def _runs(state, per_run, *, cmd="run", start=0, degraded=()):
    """按顺序写快照：per_run[i] 是第 i 轮的发现列表。批次 ID 按 6 小时递增。"""
    ids = []
    for i, fs in enumerate(per_run, start=start):
        ts = T0 + timedelta(hours=6 * i)
        rid = f"{ts:%Y%m%dT%H%M%S}.000-1"
        history.write_snapshot(state, rid, fs, cmd=cmd, degraded=i in degraded, now=ts)
        ids.append(rid)
    return ids


def test_four_consecutive_runs_make_it_stuck(tmp_path):
    ids = _runs(tmp_path, [[_layout()]] * 4)

    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)

    assert s.fp == history.fingerprint(_layout())
    assert s.runs == 4 and s.first_seen == T0.isoformat(timespec="seconds")
    assert s.kind == "season_layout_mismatch" and s.show == "胆大党"


def test_three_runs_are_not_yet_stuck(tmp_path):
    ids = _runs(tmp_path, [[_layout()]] * 3)
    assert history.find_stuck(tmp_path, ids[-1], min_runs=4) == []


def test_a_run_without_it_breaks_the_streak(tmp_path):
    ids = _runs(tmp_path, [[_layout()], [_layout()], [], [_layout()], [_layout()], [_layout()]])
    assert history.find_stuck(tmp_path, ids[-1], min_runs=4) == []
    ids += _runs(tmp_path, [[_layout()]], start=6)
    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)
    assert s.runs == 4 and s.first_seen == (T0 + timedelta(hours=18)).isoformat(timespec="seconds")


def test_streak_uses_fingerprints_not_summaries(tmp_path):
    ids = _runs(tmp_path, [[_layout(summary=f"库内有 Season [2] 而 TMDB 只有 {n} 季")]
                           for n in range(4)])
    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)
    assert s.summary.endswith("3 季")                       # 报最新这一轮的说法


def test_minor_actionless_findings_never_get_stuck(tmp_path):
    ids = _runs(tmp_path, [[_minor()]] * 10)
    assert history.find_stuck(tmp_path, ids[-1], min_runs=4) == []


def test_any_finding_with_an_action_can_get_stuck(tmp_path):
    ids = _runs(tmp_path, [[_rename()]] * 4)
    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)
    assert s.op == "rename"


def test_diagnose_snapshots_neither_count_nor_break(tmp_path):
    ids = _runs(tmp_path, [[_layout()]] * 3)
    _runs(tmp_path, [[_layout()]] * 3, cmd="diagnose", start=3)      # 手动跑了三次 diagnose
    assert history.find_stuck(tmp_path, ids[-1], min_runs=4) == []
    _runs(tmp_path, [[]], cmd="diagnose", start=6)                   # 空的 diagnose 也不打断
    ids += _runs(tmp_path, [[_layout()]], start=7)
    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)
    assert s.runs == 4


def test_degraded_runs_neither_count_nor_break(tmp_path):
    ids = _runs(tmp_path, [[_layout()], [_layout()], [], [_layout()], [_layout()]],
                degraded={2})
    [s] = history.find_stuck(tmp_path, ids[-1], min_runs=4)
    assert s.runs == 4


def test_a_degraded_current_run_reports_nothing(tmp_path):
    ids = _runs(tmp_path, [[_layout()]] * 5, degraded={4})
    assert history.find_stuck(tmp_path, ids[-1], min_runs=4) == []


# ------------------------------------------------------------------ 确认
def test_acked_fingerprint_is_marked_and_expired_ack_is_not(tmp_path):
    ids = _runs(tmp_path, [[_layout(), _layout(show="超超超超超喜欢你的100个女朋友")]] * 4)
    fp1 = history.fingerprint(_layout())
    fp2 = history.fingerprint(_layout(show="超超超超超喜欢你的100个女朋友"))
    acks = {fp1: {"reason": "要人按 TMDB 重排目录"},
            fp2: {"reason": "等下一季", "until": "2026-09-01"}}

    found = history.find_stuck(tmp_path, ids[-1], min_runs=4, acks=acks,
                               today=date(2026, 9, 26))

    by = {s.fp: s for s in found}
    assert by[fp1].ack == {"reason": "要人按 TMDB 重排目录"}
    assert by[fp2].ack is None                                    # 过期了：重新提醒


def test_ack_until_is_inclusive(tmp_path):
    acks = {"f" * 16: {"reason": "r", "until": "2026-09-26"}}
    assert history.active_ack(acks, "f" * 16, date(2026, 9, 26))
    assert not history.active_ack(acks, "f" * 16, date(2026, 9, 27))


def test_broken_acks_file_is_reported_not_fatal(tmp_path):
    p = tmp_path / "acks.json"
    p.write_text("{坏", encoding="utf-8")
    acks, problems = history.load_acks(p)
    assert acks == {} and problems and "acks.json" in problems[0]


def test_shipped_acks_cover_the_two_known_layout_mismatches(lib):
    """仓库里带的 `.agents/acks.json`：生产上今天仅有的两条会升级成"卡住"的发现
    （2026-09-26 run.log：胆大党、超超超超超喜欢你的100个女朋友的 season_layout_mismatch，
    要人重排目录）。指纹要与**检测器此刻算出来的**对得上——改了指纹算法，或检测器给这条发现加了
    path / subject，这里都会红（以前拿手搭的 Finding 算，检测器变了测不出来，复审变异 X1）。"""
    from harness import weekly

    from media_agent import config
    from media_agent.plugins.grab import EpisodeAvailableDetector
    acks, problems = history.load_acks(config.PROJECT_ROOT.parent / "_unused")
    assert problems == [] and acks == {}                          # 不存在的文件 = 没有确认

    import pathlib
    shipped = pathlib.Path(__file__).resolve().parent.parent / ".agents" / "acks.json"
    acks, problems = history.load_acks(shipped)
    assert problems == []

    # 生产的形状：库内按分季目录放（sidecar 的 have 在 Season 2 / 3 下），TMDB 只有一季
    lib.tmdb.enabled = True
    for i, (show, extra) in enumerate((("胆大党", [2]), ("超超超超超喜欢你的100个女朋友", [2, 3]))):
        sh = lib.show(show)
        seasons = {}
        for sn in [1, *extra]:
            sh.season(sn).local(f"{show} S{sn:02d}E01.mkv")
            seasons[str(sn)] = {"have": [1]}
        sh.tmdb(4000 + i, seasons={1: weekly(24, first_days_ago=200)})
        sh.sidecar(seasons=seasons)

    found = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector])
             if f.kind == "season_layout_mismatch"]

    assert sorted(f.show for f in found) == ["胆大党", "超超超超超喜欢你的100个女朋友"]
    for f in found:
        fp = history.fingerprint(f)
        assert fp in acks and acks[fp]["reason"], (f.show, fp)


# ------------------------------------------------------------------ 配置
def test_stuck_runs_config(monkeypatch):
    assert load_config().stuck_runs == 4
    monkeypatch.setenv("STUCK_RUNS", "6")
    assert load_config().stuck_runs == 6
    for bad in ("1", "0", "x", "-3"):
        monkeypatch.setenv("STUCK_RUNS", bad)
        with pytest.raises(ValueError, match="STUCK_RUNS"):
            load_config()


# ------------------------------------------------------------------ CLI：run 的输出、ack 命令
@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                fingerprint=None, reason=None, until=None, remove=False, force=False,
                list=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _seal_conflict(lib):
    from harness import video
    chi = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("尼古喵喵 S01E08.mkv", size=600_000_000, tags="ma:S01E08", probe=chi,
              name="[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
    s1.single("[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv",
              size=600_000_005, tags="ma:S01E08", probe=chi)


def _conflict_fp(lib):
    [snap] = history.load_snapshots(lib.cfg.state_dir)[-1:]
    [rec] = [r for r in snap.findings if r["kind"] == "seal_conflict"]
    return rec["fp"]


def test_run_reports_stuck_findings_from_the_fourth_run(offline_cli, capsys):
    lib = offline_cli
    _seal_conflict(lib)
    for _ in range(3):
        cli.cmd_run(_args(), lib.cfg)
    assert "卡住" not in capsys.readouterr().out

    cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr().out
    fp = _conflict_fp(lib)
    assert "卡住" in out and fp in out and "连续 4 轮" in out
    assert f"media-agent ack {fp}" in out


def test_acked_stuck_finding_is_only_counted(offline_cli, capsys):
    lib = offline_cli
    _seal_conflict(lib)
    cli.cmd_run(_args(), lib.cfg)
    fp = _conflict_fp(lib)
    capsys.readouterr()
    assert cli.cmd_ack(_args(fingerprint=fp, reason="两个版本都要，等人挑"), lib.cfg) == 0
    capsys.readouterr()                                           # ack 自己的输出里有指纹
    for _ in range(3):
        cli.cmd_run(_args(), lib.cfg)

    out = capsys.readouterr().out
    # 指纹只出现在「⏳」下一行的「指纹 …——media-agent ack …」里：确认过的一个字都不该列出来。以前断言
    # "⏳ 那一行里没有指纹"永远成立、「1 个已确认」又被健康报告的「卡住 0（另 1 个已确认）」满足，
    # 把确认过的当未确认列出的变异（H2o）照样绿（2026-09-26 复审）
    assert fp not in out
    assert not [ln for ln in out.splitlines() if ln.lstrip().startswith("⏳")]
    assert "═══ 卡住：没有未确认的（1 个已确认、不再提醒" in out


def test_ack_command_writes_the_versioned_file_and_says_commit_it(offline_cli, capsys):
    lib = offline_cli
    _seal_conflict(lib)
    cli.cmd_run(_args(), lib.cfg)
    fp = _conflict_fp(lib)
    capsys.readouterr()

    rc = cli.cmd_ack(_args(fingerprint=fp[:8], reason="等人挑", until="2026-12-31"), lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(history.acks_path().read_text(encoding="utf-8"))
    assert data[fp]["reason"] == "等人挑" and data[fp]["until"] == "2026-12-31"
    assert "seal_conflict" in data[fp]["what"] and "尼古喵喵#S01E08" in data[fp]["what"]
    assert "git" in out and "acks.json" in out                     # 提醒提交入库

    assert cli.cmd_ack(_args(fingerprint=fp, remove=True), lib.cfg) == 0
    assert fp not in json.loads(history.acks_path().read_text(encoding="utf-8"))


@pytest.mark.parametrize("kw, msg", [
    (dict(fingerprint="0123456789abcdef", reason="x"), "发现历史里没有"),
    (dict(fingerprint="zz", reason="x"), "指纹"),
    (dict(fingerprint="SEAL", reason=""), "--reason"),
    (dict(fingerprint="SEAL", reason="x", until="2026/12/31"), "YYYY-MM-DD"),
])
def test_ack_command_refuses_bad_input(offline_cli, capsys, kw, msg):
    lib = offline_cli
    _seal_conflict(lib)
    cli.cmd_run(_args(), lib.cfg)
    if kw.get("fingerprint") == "SEAL":
        kw["fingerprint"] = _conflict_fp(lib)
    capsys.readouterr()

    assert cli.cmd_ack(_args(**kw), lib.cfg) != 0
    assert msg in capsys.readouterr().out
    assert not history.acks_path().exists()


def test_ack_force_allows_a_fingerprint_not_seen_yet(offline_cli, capsys):
    lib = offline_cli
    assert cli.cmd_ack(_args(fingerprint="0123456789abcdef", reason="预先确认",
                             force=True), lib.cfg) == 0
    assert "0123456789abcdef" in json.loads(history.acks_path().read_text(encoding="utf-8"))
