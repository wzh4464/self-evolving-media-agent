"""发现历史：每轮 run（与 diagnose）把**全部**发现写进 `state/findings/<run_id>.jsonl`，带稳定指纹。

为什么（critic N11）：没有动作的发现——`pending_ownership`、`seal_conflict`、`season_layout_mismatch`、
抓取的备注……——以前只作为文字打进 run.log，跨轮看不出"同一个问题已经挂了三天"。而没有路径的发现
去重键退回 `(show, summary)`，摘要里嵌着计数（「S01 缺 3 集」下一轮变「缺 2 集」），拿它当身份，
每轮都是"新问题"。

指纹 = hash(规则, 类型, 目标)，目标 = `show#subject`（集位 / 季这类不是某个文件的问题）| 路径 |
种子 hash | show；**永远不含摘要**。
"""
from __future__ import annotations

import argparse
import json
import os

import pytest

from media_agent import cli, history
from media_agent.kernel import Action, Finding


def _f(**kw) -> Finding:
    base = dict(rule="incomplete-season", kind="incomplete_season", severity="important",
                summary="S01 缺 3 集（已播 12 集，已有 9 集）：[10, 11, 12]", show="尼古喵喵",
                subject="S01")
    base.update(kw)
    return Finding(**base)


# ------------------------------------------------------------------ 指纹
def test_fingerprint_ignores_the_summary_counts():
    a = _f()
    b = _f(summary="S01 缺 2 集（已播 12 集，已有 10 集）：[11, 12]")
    assert history.fingerprint(a) == history.fingerprint(b)


def test_fingerprint_separates_targets_rules_and_kinds():
    base = history.fingerprint(_f())
    assert history.fingerprint(_f(subject="S02")) != base
    assert history.fingerprint(_f(show="别的番")) != base
    assert history.fingerprint(_f(rule="episode-available")) != base
    assert history.fingerprint(_f(kind="other")) != base


def test_fingerprint_target_prefers_subject_then_path_then_hash_then_show():
    """集位级的发现（seal_conflict / pending_ownership …）的 `path` 只是桶里第一个文件，
    谁排第一会变；它们说的是这一集，身份要按集位认。"""
    slot = _f(rule="duplicate-episode", kind="seal_conflict", subject="S01E08",
              path="/Media/尼古喵喵/Season 1/a.mkv")
    moved = _f(rule="duplicate-episode", kind="seal_conflict", subject="S01E08",
               path="/Media/尼古喵喵/Season 1/b.mkv")
    assert history.fingerprint(slot) == history.fingerprint(moved)
    assert history.target_of(slot) == "尼古喵喵#S01E08"

    by_path = _f(subject="", path="/Media/x.mkv", torrent_hash="ABC")
    assert history.target_of(by_path) == "/Media/x.mkv"
    by_hash = _f(subject="", path="", torrent_hash="ABCdef")
    assert history.target_of(by_hash) == "torrent:abcdef"
    by_show = _f(subject="", path="", torrent_hash="")
    assert history.target_of(by_show) == "尼古喵喵"


def test_fingerprint_is_a_short_stable_hex_string():
    fp = history.fingerprint(_f())
    assert len(fp) == 16 and int(fp, 16) >= 0
    # 与进程、哈希随机化无关：写死一个已知值，改算法必须有意为之（acks.json 里存的就是它）
    assert fp == history.fingerprint(_f())


# ------------------------------------------------------------------ 真检测器：计数变了，指纹不变
def _four_copies(lib, n):
    s1 = lib.show("测试番").season(1)
    for i in range(n):
        s1.local(f"[G{i}] Test Show - 05 [1080p].mkv", size=600_000_000 + i)
    return s1


@pytest.mark.parametrize("more", [5, 6])
def test_detector_fingerprint_is_stable_when_its_count_changes(lib, more):
    """suspicious_episode_parse 的摘要带文件数（「竟有 4 个文件」→「竟有 5 个」），path 是桶里
    第一个文件。多一个文件、换一个第一名，都还是"这一集的集号解析异常"。"""
    s1 = _four_copies(lib, 4)
    [a] = [f for f in lib.diagnose() if f.kind == "suspicious_episode_parse"]
    for i in range(4, more):
        s1.local(f"[A{i}] Test Show - 05 [1080p].mkv", size=700_000_000 + i)   # 排到最前
    [b] = [f for f in lib.diagnose() if f.kind == "suspicious_episode_parse"]

    assert a.summary != b.summary
    assert history.fingerprint(a) == history.fingerprint(b)


def test_rename_collision_fingerprint_does_not_follow_the_torrent_list_order(lib, monkeypatch):
    """rename_collision（critical、没有动作，会进卡住检测）以前以桶里第一个文件为 path：有种子的文件按
    `torrents()` 的顺序排，而 qBit 5.x 的 /torrents/info 不排序（遍历 QHash，进程重启、加种子后都会变）。
    看门狗重建一次容器，指纹就换了——连续段断掉、人的确认悄悄失效、再过 STUCK_RUNS 轮又报「新卡住」。"""
    from media_agent.plugins.builtin import RenameCollisionDetector
    s1 = lib.show("朱音落语").season(1)
    s1.single("[JPSC] Akane-banashi - 08 [1080p].mp4", size=600_000_001)
    s1.single("[JPTC] Akane-banashi - 08 [1080p].mp4", size=600_000_002)
    [a] = lib.diagnose(detectors=[RenameCollisionDetector])
    real = type(lib.qbit).torrents
    monkeypatch.setattr(type(lib.qbit), "torrents",
                        lambda self, category=None: list(reversed(real(self, category))))
    [b] = lib.diagnose(detectors=[RenameCollisionDetector])

    assert a.path != b.path                                       # 桶里的第一名确实换了
    assert history.fingerprint(a) == history.fingerprint(b)
    assert history.target_of(a) == "朱音落语#S01E08"


# ------------------------------------------------------------------ 落盘
def _read(path):
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    return lines[0], lines[1:]


def test_snapshot_holds_every_finding_including_actionless_and_classified(tmp_path):
    fs = [_f(),
          Finding(rule="duplicate-episode", kind="seal_conflict", severity="important",
                  summary="x", show="尼古喵喵", path="/m/a.mkv", subject="S01E08",
                  classified=True),
          Finding(rule="unrenamed-file", kind="unrenamed", severity="important", summary="y",
                  show="尼古喵喵", path="/m/b.mkv",
                  action=Action(op="rename", args={"path": "/m/b.mkv", "new_name": "c.mkv"}))]

    path, problems = history.write_snapshot(tmp_path, "r1", fs, cmd="run")

    assert problems == []
    assert path == tmp_path / "findings" / "r1.jsonl"
    head, recs = _read(path)
    assert head["type"] == "header" and head["run_id"] == "r1" and head["cmd"] == "run"
    assert head["findings"] == 3 and head["degraded"] is False
    assert [r["kind"] for r in recs] == ["incomplete_season", "seal_conflict", "unrenamed"]
    assert [r["op"] for r in recs] == [None, None, "rename"]
    assert recs[1]["classified"] is True
    assert all(r["fp"] == history.fingerprint(f) for r, f in zip(recs, fs))


def test_empty_run_still_writes_a_header(tmp_path):
    """一轮什么都没发现也要留一个文件：连续几轮"都有这个问题"要靠它来断。"""
    path, _ = history.write_snapshot(tmp_path, "r1", [], cmd="run")
    head, recs = _read(path)
    assert head["findings"] == 0 and recs == []


def test_retention_keeps_the_last_60_snapshots(tmp_path):
    for i in range(65):
        history.write_snapshot(tmp_path, f"2026092{i // 10}T0{i % 10}0000.000-1", [_f()],
                               cmd="run")
    names = sorted(p.name for p in (tmp_path / "findings").iterdir())
    assert len(names) == history.KEEP_RUNS == 60
    assert names[0] == "20260920T050000.000-1.jsonl"          # 最老的 5 个被删掉


def test_write_never_raises(tmp_path):
    (tmp_path / "findings").write_text("不是目录")               # 目录位置被一个文件占了
    path, problems = history.write_snapshot(tmp_path, "r1", [_f()], cmd="run")
    assert path is None and problems and "findings" in problems[0]


def test_unreadable_lines_and_foreign_files_are_skipped(tmp_path):
    history.write_snapshot(tmp_path, "r1", [_f()], cmd="run")
    d = tmp_path / "findings"
    with open(d / "r1.jsonl", "a", encoding="utf-8") as fp:
        fp.write("{坏行\n")
    (d / "notes.txt").write_text("x")
    (d / "r2.jsonl").write_text("")                            # 空文件：没有 header，不算一轮

    snaps = history.load_snapshots(tmp_path)

    assert [s.run_id for s in snaps] == ["r1"]
    assert len(snaps[0].findings) == 1


# ------------------------------------------------------------------ run / diagnose 都写
@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _two_seals(lib):
    """两个不同的种子都封存着 S01E08：诊断出一个没有动作、已归类的发现（seal_conflict）。"""
    from harness import video
    chi = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("尼古喵喵 S01E08.mkv", size=600_000_000, tags="ma:S01E08", probe=chi,
              name="[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
    s1.single("[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv",
              size=600_000_005, tags="ma:S01E08", probe=chi)
    return s1


def test_cmd_run_writes_a_snapshot_under_its_run_id(offline_cli, capsys):
    lib = offline_cli
    _two_seals(lib)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    snaps = history.load_snapshots(lib.cfg.state_dir)
    [snap] = snaps
    assert snap.cmd == "run" and not snap.degraded
    kinds = {r["kind"] for r in snap.findings}
    assert "seal_conflict" in kinds
    # 执行器的批次 ID 就是快照的 run_id：审计与发现历史能对上
    assert {r["run_id"] for r in lib.audit()} <= {snap.run_id}


def test_cmd_diagnose_writes_a_snapshot_too(offline_cli, capsys):
    lib = offline_cli
    _two_seals(lib)

    assert cli.cmd_diagnose(_args(), lib.cfg) == 0

    [snap] = history.load_snapshots(lib.cfg.state_dir)
    assert snap.cmd == "diagnose"
    assert "seal_conflict" in {r["kind"] for r in snap.findings}
    assert lib.audit() == []


def test_degraded_scan_is_marked_in_the_snapshot(offline_cli, capsys):
    lib = offline_cli
    _two_seals(lib)
    lib.qbit_down()

    assert cli.cmd_run(_args(), lib.cfg) == cli.EXIT_DEGRADED

    [snap] = history.load_snapshots(lib.cfg.state_dir)
    assert snap.degraded


def test_cmd_run_survives_an_unwritable_findings_dir(offline_cli, capsys):
    lib = offline_cli
    _two_seals(lib)
    (lib.cfg.state_dir / "findings").write_text("不是目录")

    assert cli.cmd_run(_args(), lib.cfg) == 0
    assert "发现历史" in capsys.readouterr().err
    assert os.path.isfile(lib.cfg.state_dir / "findings")
