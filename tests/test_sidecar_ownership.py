"""sidecar 的字段归属：谁算的、谁说了算，写的时候各管各的。

为什么（2026-09-26 状态测绘）：`.media-agent.json` 里混着两种东西——
- **派生**的：sidecar-sync / 抓取按盘上、AB、TMDB 算出来的（各季 `have` / `aired` / `total` / `next_air`、
  当前源、别名……），下一轮算错了下一轮再算；
- **人的意图**：`season_offsets`、`require_any`、`notes`，以及人钉住的东西。代码从不写它们，丢了就是丢了。

`_op_write_sidecar` 以前拿诊断期的快照**整份覆盖**：诊断之后、写之前人改的 `season_offsets`、另一个进程写的、
回退写回去的，全被盖掉；`load()` 还丢掉不认识的键。更糟的是 `load()` 遇到坏 JSON 静默返回默认值——下一次写
就拿默认值盖掉那份文件，人写的偏移、版本要求一起没了。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import weekly

from media_agent import sidecar as sc_mod
from media_agent.kernel import Action, Finding

SHOW = "药屋少女的呢喃"


def _write_finding(show_dir: Path, payload: dict) -> Finding:
    return Finding(rule="sidecar-sync", kind="sidecar_stale", severity="minor",
                   summary="采集档案需更新", show=show_dir.name,
                   action=Action(op="write_sidecar",
                                 args={"show_dir": str(show_dir), "payload": payload}))


def _raw(show_dir: Path) -> dict:
    return json.loads(sc_mod.path_for(show_dir).read_text(encoding="utf-8"))


@pytest.fixture
def show(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in (1, 2, 3):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    return sh


# ------------------------------------------------------------------ 归属表
def test_every_sidecar_field_has_exactly_one_owner():
    owners = [sc_mod.DERIVED, sc_mod.IDENTITY, sc_mod.USER_INTENT, sc_mod.BOOKKEEPING]
    fields = set(sc_mod.Sidecar.__dataclass_fields__)
    assert set().union(*owners) == fields
    assert sum(len(o) for o in owners) == len(fields)          # 没有一个字段归两家


# ------------------------------------------------------------------ write_sidecar 只写派生字段
def test_write_sidecar_keeps_user_intent_written_after_diagnose(lib, show):
    show.sidecar(seasons={"1": {"have": [1]}})
    [f] = [f for f in lib.diagnose() if f.action and f.action.op == "write_sidecar"]
    # 诊断之后、执行之前：人在 sidecar 里登记了换算关系、版本要求、备注，还有个本版本不认识的键
    raw = _raw(show.path)
    raw.update(season_offsets={"2": 24}, require_any=["无删减"], notes=["S2 按 TMDB 压平"],
               future_field={"kept": True})
    sc_mod.path_for(show.path).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    rep = lib.apply([f])

    [rec] = rep.applied
    after = _raw(show.path)
    assert after["seasons"]["1"]["have"] == [1, 2, 3]            # 派生的照写
    assert after["season_offsets"] == {"2": 24}
    assert after["require_any"] == ["无删减"]
    assert after["notes"] == ["S2 按 TMDB 压平"]
    assert after["future_field"] == {"kept": True}               # 不认识的键不丢
    assert rec["undo"]["op"] == "restore_sidecar"


def test_payload_user_intent_never_overwrites_the_file(lib, show):
    """payload 里带的用户字段是诊断期读到的旧值——哪怕它与此刻不同，也不拿它写。"""
    show.sidecar(season_offsets={"2": 24}, require_any=["无删减"])

    lib.apply([_write_finding(show.path, {"canonical_title": SHOW, "season_offsets": {},
                                          "require_any": [], "notes": ["旧"]})])

    after = _raw(show.path)
    assert after["season_offsets"] == {"2": 24} and after["require_any"] == ["无删减"]
    assert after["notes"] == []


def test_existing_tmdb_id_is_never_changed_by_code(lib, show):
    """TMDB 身份：代码只在还没有时填一次；填上之后只有人改（扫描照它认，见 TMDB 钉住）。"""
    show.sidecar(tmdb_id=1201, tmdb_title="物语系列")

    lib.apply([_write_finding(show.path, {"tmdb_id": 99, "tmdb_title": "别的番",
                                          "tmdb_source": "search"})])

    after = _raw(show.path)
    assert (after["tmdb_id"], after["tmdb_title"]) == (1201, "物语系列")


def test_sidecar_sync_does_not_propose_a_different_tmdb_id(lib, show):
    """按目录名能搜到另一个条目（以前缓存过期后重新搜、就可能选中它）：档案里的身份不跟着换。"""
    show.sidecar(tmdb_id=111, tmdb_title=SHOW, seasons={"1": {"have": [1, 2, 3]}})
    show.tmdb(222, title=SHOW, seasons={1: weekly(3, first_days_ago=400)})
    lib.tmdb.add_show(111, SHOW, seasons={1: weekly(3, first_days_ago=400)})

    c = lib.cycle()

    assert c.state.shows[0].tmdb_id == 111
    assert _raw(show.path)["tmdb_id"] == 111


def test_absent_tmdb_id_is_filled(lib, show):
    lib.apply([_write_finding(show.path, {"tmdb_id": 99, "tmdb_title": "药屋",
                                          "tmdb_source": "search"})])

    after = _raw(show.path)
    assert (after["tmdb_id"], after["tmdb_title"], after["tmdb_source"]) == (99, "药屋", "search")


def test_a_pinned_derived_field_is_left_alone(lib, show):
    show.sidecar(tmdb_id=1, tmdb_title="人定的标题", pinned=["tmdb_title"])

    lib.apply([_write_finding(show.path, {"tmdb_id": 1, "tmdb_title": "TMDB 的标题"})])

    assert _raw(show.path)["tmdb_title"] == "人定的标题"


def test_aliases_are_merged_not_replaced(lib, show):
    show.sidecar(aliases=["Kusuriya no Hitorigoto"])

    lib.apply([_write_finding(show.path, {"aliases": ["The Apothecary Diaries"]})])

    assert _raw(show.path)["aliases"] == ["Kusuriya no Hitorigoto", "The Apothecary Diaries"]


def test_write_sidecar_skips_when_the_show_dir_moved_this_run(lib, show):
    """目录改名（op 8）排在写档案（op 10）之前：诊断期的路径已经不在了——跳过，不是失败。"""
    f = _write_finding(show.path, {"canonical_title": SHOW})
    lib.fs_move(SHOW, "药屋少女的独语")

    rep = lib.apply([f])

    assert not rep.failed and not rep.applied
    [rec] = rep.skipped
    assert "不在" in rec["reason"]
    assert not show.path.exists()


# ------------------------------------------------------------------ 坏掉的 sidecar
BROKEN = '{"season_offsets": {"2": 24}, "require_any": ["无删减"'          # 人手改到一半


def _break(show) -> bytes:
    p = sc_mod.path_for(show.path)
    p.write_text(BROKEN, encoding="utf-8")
    return p.read_bytes()


def _backups(show) -> list[Path]:
    return sorted(show.path.glob(sc_mod.SIDECAR_NAME + ".corrupt-*"))


def test_load_checked_reports_corrupt_json(show):
    _break(show)
    sc, problem = sc_mod.load_checked(show.path)
    assert problem and sc.season_offsets == {}
    assert sc_mod.load(show.path).canonical_title == SHOW          # 读的一方照旧拿到默认值继续


@pytest.mark.parametrize("text", ["[1, 2]", '"just a string"', "null"])
def test_json_that_is_not_an_object_is_corrupt_too(show, text):
    sc_mod.path_for(show.path).write_text(text, encoding="utf-8")
    sc, problem = sc_mod.load_checked(show.path)
    assert problem
    assert sc_mod.load(show.path).seasons == {}                    # 以前 `data.items()` 直接抛


def test_corrupt_sidecar_is_reported_backed_up_and_never_overwritten(lib, show):
    before = _break(show)

    c = lib.cycle()

    [bad] = [f for f in c.findings if f.kind == "sidecar_corrupt"]
    assert bad.severity == "important" and bad.show == SHOW
    [rec] = c.skipped("write_sidecar")
    assert "解析不了" in rec["reason"]
    assert sc_mod.path_for(show.path).read_bytes() == before
    [bk] = _backups(show)
    assert bk.read_bytes() == before

    lib.cycle()                                                    # 下一轮：还是不写，也不重复备份
    assert sc_mod.path_for(show.path).read_bytes() == before
    assert _backups(show) == [bk]


def test_fixed_sidecar_is_written_again(lib, show):
    _break(show)
    lib.cycle()
    sc_mod.path_for(show.path).write_text('{"season_offsets": {"2": 24}}', encoding="utf-8")

    c = lib.cycle()

    assert not [f for f in c.findings if f.kind == "sidecar_corrupt"]
    assert c.applied("write_sidecar")
    after = _raw(show.path)
    assert after["season_offsets"] == {"2": 24} and after["seasons"]["1"]["have"] == [1, 2, 3]


@pytest.mark.allow("log_failure", match="写 sidecar 出错")
def test_grab_does_not_clobber_a_corrupt_sidecar(lib, show):
    """抓取的记账是读-改-写：坏文件以前被读成默认值、再整份写回——人写的偏移就这么没了。"""
    before = _break(show)
    title = "[LoliHouse] 药屋少女的呢喃 / Kusuriya no Hitorigoto - 04 [WebRip 1080p][简繁内封字幕]"
    url, h = lib.web.torrent(title)
    grab = Finding(rule="episode-available", kind="episode_grabbable", severity="important",
                   summary="S01E04 可抓取", show=SHOW,
                   action=Action(op="grab_episode", args={
                       "url": url, "title": title, "show_dir": str(show.path), "season": 1,
                       "episode": 4, "bangumi_id": None, "category": SHOW, "official_title": SHOW}))

    rep = lib.apply([grab])

    [rec] = rep.applied                                            # 种子加上了：抓取本身照常
    assert lib.qbit.has(h)
    assert "sidecar_error" in rec
    assert sc_mod.path_for(show.path).read_bytes() == before
    assert _backups(show)


def test_rollback_does_not_overwrite_a_sidecar_that_broke_since(lib, show):
    show.sidecar(seasons={"1": {"have": [1]}})
    lib.apply([_write_finding(show.path, {"canonical_title": SHOW})], run_id="w1")
    before = _break(show)

    res = lib.rollback("w1")

    assert (res["reverted"], res["skipped"], res["failed"]) == (0, 1, 0)
    assert "解析不了" in res["skipped_detail"][0]["skip_reason"]
    assert sc_mod.path_for(show.path).read_bytes() == before
    assert _backups(show)


def test_run_health_warns_about_a_corrupt_sidecar(lib, show, monkeypatch):
    import argparse

    from media_agent import cli, health
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True
    _break(show)
    args = argparse.Namespace(dry_run=False, no_tmdb=True, no_evolve=True, max_proposals=0,
                              json=False)

    cli.cmd_run(args, lib.cfg)

    rep = health.load_report(lib.cfg.state_dir)
    [r] = [r for r in rep["reasons"] if r["code"] == "sidecar_corrupt"]
    assert r["level"] == "warn" and SHOW in r["text"]
