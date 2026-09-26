"""第 3 阶段清理出来的被吞异常：以前一声不吭，现在各自说一句（日志带上下文），行为不变；健康报告数得到。"""
from __future__ import annotations

import argparse
import json

import pytest

from media_agent import cli, health
from media_agent.kernel import load_rule_specs
from media_agent.plugins.builtin import StaleTorrentPathDetector

SIZE = 734_003_200
OLD = "GNOSIA - S01E08 [WebRip 1080p HEVC-10bit AAC].mkv"
SLOT = "古诺希亚 S01E08.mkv"


@pytest.mark.allow("log_failure", match=r"\[scan\] TMDB 取标题")
def test_tmdb_title_lookup_failure_is_logged_and_the_show_stays_unmatched(lib, tripwire,
                                                                           monkeypatch):
    s = lib.show("测试番")
    s.season(1).local("测试番 S01E01.mkv", size=1000)
    s.tmdb(42, title="测试番", seasons={1: [(1, "2026-01-01")]})

    def boom(tv_id):
        raise TimeoutError("timed out")

    monkeypatch.setattr(lib.tmdb, "official_title", boom)

    state = lib.scan()

    assert state.shows[0].tmdb_id is None                         # 行为不变：按没匹配处理
    [ev] = tripwire.of("log_failure")
    assert "测试番" in ev.detail and "id 42" in ev.detail and "TimeoutError" in ev.detail


@pytest.mark.allow("log_failure", match=r"\[rollback\] 回退 relink")
def test_relink_undo_that_cannot_rename_back_says_so(lib, tripwire):
    s1 = lib.show("古诺希亚").season(1)
    t = s1.single(OLD, size=SIZE, on_disk=False)
    s1.local(SLOT, size=SIZE)
    c = lib.cycle(detectors=[StaleTorrentPathDetector])
    assert c.applied("relink_torrent")
    lib.qbit.fail("rename_file", hash=t.hash, times=None)

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1                                   # 行为不变
    [ev] = tripwire.of("log_failure")
    assert t.hash[:8] in ev.detail and SLOT in ev.detail and "改回失败" in ev.detail


def test_malformed_rule_file_is_reported_not_silently_dropped(tmp_path):
    (tmp_path / "a-good.json").write_text(json.dumps(
        {"id": "a-good", "kind": "k", "match": {"field": "ext", "op": "eq", "value": ".mkv"}}),
        encoding="utf-8")
    (tmp_path / "b-bad.json").write_text("{坏", encoding="utf-8")
    (tmp_path / "c-nomatch.json").write_text(json.dumps({"id": "c", "kind": "k"}),
                                             encoding="utf-8")
    errors: list = []

    specs = load_rule_specs(tmp_path, errors=errors)

    assert [s.id for s in specs] == ["a-good"]                    # 其余照常挂上
    assert [e.split("：")[0] for e in errors] == ["b-bad.json", "c-nomatch.json"]


def test_build_registry_logs_rule_load_failures(monkeypatch, capsys, project_root):
    from media_agent import evolution
    rules = project_root / ".agents" / "rules"
    rules.mkdir(parents=True)
    (rules / "broken.json").write_text("{坏", encoding="utf-8")
    monkeypatch.setattr(evolution, "RULES_DIR", rules)

    reg = cli.build_registry()

    assert reg.load_errors and "broken.json" in reg.load_errors[0]
    assert "演进规则加载失败" in capsys.readouterr().err


@pytest.mark.allow("log_failure", match=r"\[scan\] TMDB 取标题")
def test_logged_swallowed_errors_are_counted_in_the_health_report(lib, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    s = lib.show("测试番")
    s.season(1).local("测试番 S01E01.mkv", size=1000)
    s.tmdb(42, title="测试番", seasons={1: [(1, "2026-01-01")]})
    monkeypatch.setattr(lib.tmdb, "official_title",
                        lambda tv_id: (_ for _ in ()).throw(TimeoutError("timed out")))

    cli.cmd_run(argparse.Namespace(dry_run=False, no_tmdb=False, no_evolve=False,
                                   max_proposals=0), lib.cfg)

    le = health.load_report(lib.cfg.state_dir)["logged_errors"]
    assert le["count"] == 1 and le["by_tag"] == {"scan": 1}
    assert "TMDB 取标题" in le["samples"][0]
    assert "1 行报错（[scan] 1）" in capsys.readouterr().out
