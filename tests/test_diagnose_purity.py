"""`diagnose` / `apply --dry-run` 不改媒体根下的任何东西。

为什么（2026-09-26 状态测绘）：抓取检测器 `_resolve_mikan_id` 在**检测期**把选中的番组页 id 写回 sidecar
（`save_sidecar`）。于是 `diagnose`、`apply --dry-run`、演进器的影子验证全都会改写 `.media-agent.json`——生产上
同一天 12:01–12:02 有 5 份 sidecar 被改写，而最后一条审计停在 11:17：改动没有任何审计记录，也回退不了。
"只读"的命令只许往 `state/` 里写（发现历史、缓存），媒体根下一个字节都不能动。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from harness import MikanItem, weekly

from media_agent import cli
from media_agent import sidecar as sc_mod
from media_agent.cache import Cache

SHOW = "尼古喵喵"


def _media_state(root: Path) -> dict[str, tuple]:
    """媒体根下每个条目的 (类型, 大小, mtime_ns, 小文件的内容)。媒体文件是稀疏大文件，只比元数据。"""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        body = p.read_bytes() if p.is_file() and st.st_size < 1_000_000 else None
        out[str(p.relative_to(root))] = (p.is_dir(), st.st_size, st.st_mtime_ns, body)
    return out


def _grab_scene(lib) -> None:
    """一部在播番：S01 已有 1–7，第 8、9 集可抓；sidecar 里还没记番组页 id（检测期会去搜、选一个）。
    另有一个发布名的种子等着改名、一份过期的 sidecar 等着同步——都是只该在执行阶段动手的。"""
    lib.tmdb.enabled = True
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 7):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    s1.single("[LoliHouse] Yani Neko - 07 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕].mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(seasons={"1": {"have": list(range(1, 8))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    lib.mikan("3500", [MikanItem(title=title.format(n), pub=dict(schedule)[n]) for n in (8, 9)],
              search=[SHOW])


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=False, no_evolve=True, max_proposals=0, json=False,
                kind=None, show=None, limit=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    return lib


def test_diagnose_writes_nothing_under_the_media_root(offline_cli, capsys):
    lib = offline_cli
    _grab_scene(lib)
    before = _media_state(lib.media_root)

    assert cli.cmd_diagnose(_args(), lib.cfg) == 0

    out = capsys.readouterr().out
    assert "可抓取" in out                                     # 抓取检测器确实跑到了选番组页那一步
    assert _media_state(lib.media_root) == before
    assert sc_mod.load(lib.media_root / SHOW).mikan_id == ""


def test_apply_dry_run_writes_nothing_under_the_media_root(offline_cli, capsys):
    lib = offline_cli
    _grab_scene(lib)
    before = _media_state(lib.media_root)

    assert cli.cmd_apply(_args(dry_run=True), lib.cfg) == 0

    assert "【预演】" in capsys.readouterr().out
    assert _media_state(lib.media_root) == before


def test_the_chosen_mikan_page_is_remembered_outside_the_media_root(lib):
    """选中的番组页照样记下来（下一轮不必从头搜），只是记在 state/ 的缓存里；发现里写明用的是哪一页。"""
    _grab_scene(lib)

    first = [f for f in lib.diagnose() if f.action and f.action.op == "grab_episode"]
    # 搜索缓存过期、再搜也搜不到了（Mikan 的搜索结果常变）：只能靠记住的那一页
    Cache(lib.cfg.cache_db).conn.execute("DELETE FROM llm WHERE key LIKE 'mikansearch:%'").connection.commit()
    lib.web.mikan_search(SHOW, [])
    again = [f for f in lib.diagnose() if f.action and f.action.op == "grab_episode"]

    assert {f.subject for f in first} == {f.subject for f in again} == {"S01E08", "S01E09"}
    assert {f.evidence["mikan_id"] for f in first + again} == {"3500"}
    assert sc_mod.load(lib.media_root / SHOW).mikan_id == ""   # 不写进 sidecar
