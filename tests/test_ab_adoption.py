"""把 AutoBangumi 订阅行上的集号偏移迁进 sidecar（`ab-adoption` / `adopt_episode_offset`）。

AB 37《超超超超超喜欢你的100个女朋友》第三季的 `episode_offset -24` 只记在 AB 库里：规则在迁移之前退回 AB 行
（`builtin.episode_offset_for`），AB 一退役（行删掉、停用成 `deleted=1`）就没了。迁移是一个**动作**：只在 sidecar
这一季还没有登记时提议；经执行器按此刻的文件写（只动这一项，别的字段、不认识的键都在）；有审计、能回退——回退只摘
本动作写下的那一项，人后来改过的不动。检测只读（AGENTS.md 第 13 条）。AB 库用真的 sqlite（`lib.bangumi`）。
"""
from __future__ import annotations

import json
import sqlite3

from media_agent import sidecar as sc_mod
from media_agent.plugins.adopt import AbAdoptionDetector
from media_agent.plugins.builtin import _resolve

TITLE = "超超超超超喜欢你的100个女朋友"
RAW_25 = "[Nekomoe kissaten] Hyakkano - 25 [1080p][JPSC].mkv"


def _scene(lib, *, sidecar: dict | None = None, **row):
    """库里有第三季（一个本地文件，sidecar-sync 记进了 `seasons`）；AB 订阅 37 带着 -24。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(TITLE)
    sh.season(3).local(f"{TITLE} S03E02.mkv")
    sh.sidecar(**{"seasons": {"3": {"have": [2]}}, **(sidecar or {})})
    row.setdefault("season", 3)
    row.setdefault("episode_offset", -24)
    sh.bangumi(37, title_raw="Hyakkano", **row)
    return sh


def _proposals(lib):
    return [f for f in lib.diagnose(detectors=[AbAdoptionDetector]) if f.action]


def test_the_ab_offset_is_proposed_while_the_sidecar_lacks_it(lib):
    _scene(lib)

    [f] = _proposals(lib)

    assert (f.rule, f.kind, f.subject) == ("ab-adoption", "ab_episode_offset", "S03")
    assert f.action.op == "adopt_episode_offset"
    assert f.action.args == {"show_dir": str(lib.path(TITLE)), "season": 3, "offset": -24,
                             "bangumi_id": 37}


def test_applying_writes_only_that_entry_and_converges(lib):
    sh = _scene(lib, sidecar={"season_offsets": {"1": 0}, "require_any": ["邪竜解放版"],
                              "notes": ["人写的"], "episode_offsets": {"1": 3}})
    p = sc_mod.path_for(sh.path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    p.write_text(json.dumps({**raw, "from_the_future": 1}, ensure_ascii=False), encoding="utf-8")

    c = lib.cycle(detectors=[AbAdoptionDetector])

    [rec] = c.applied("adopt_episode_offset")
    assert rec["undo"] == {"op": "unset_sidecar", "show_dir": str(sh.path),
                           "entries": [{"field": "episode_offsets", "key": "3", "value": -24}]}
    now = json.loads(p.read_text(encoding="utf-8"))
    assert now["episode_offsets"] == {"1": 3, "3": -24}
    assert (now["season_offsets"], now["require_any"], now["notes"], now["from_the_future"]) == (
        {"1": 0}, ["邪竜解放版"], ["人写的"], 1)
    assert not _proposals(lib)


def test_an_existing_entry_is_never_touched(lib):
    """人写的 `{"3": 0}`（这一季不换算）：不提议，不管 AB 行上是什么。"""
    _scene(lib, sidecar={"episode_offsets": {"3": 0}})

    assert not _proposals(lib)


def test_the_entry_appearing_after_diagnose_wins(lib):
    """诊断之后人写上了这一季：执行时按此刻的文件，不覆盖。"""
    sh = _scene(lib)
    findings = lib.diagnose(detectors=[AbAdoptionDetector])
    sh.sidecar(episode_offsets={"3": -23})

    rep = lib.apply(findings)

    [rec] = rep.skipped
    assert "已有" in rec["reason"]
    assert lib.sidecar(TITLE).episode_offsets == {"3": -23}


def test_rows_without_an_offset_or_disabled_are_left_alone(lib):
    _scene(lib, episode_offset=0)
    lib.bangumi(id=38, official_title=TITLE, title_raw="Hyakkano S2", season=2, episode_offset=-12,
                save_path=str(lib.path(TITLE) / "Season 2"), deleted=True)

    assert not _proposals(lib)


def test_the_season_offset_decides_which_library_season_gets_it(lib):
    sh = lib.show(TITLE)
    sh.season(3).local(f"{TITLE} S03E02.mkv")
    sh.sidecar(seasons={"3": {"have": [2]}})
    lib.configure(qbit_allow_empty=True)
    lib.bangumi(id=37, official_title=TITLE, title_raw="Hyakkano", season=2, season_offset=1,
                episode_offset=-24, save_path=str(sh.path / "Season 3"))

    [f] = _proposals(lib)

    assert f.action.args["season"] == 3


def test_a_row_whose_show_dir_does_not_exist_carries_the_offset_into_the_new_dir(lib):
    """目录都还没有：不单独迁偏移，它随订阅接手建目录时一起写（`test_ab_subscriptions.py`）。诊断不建目录。"""
    lib.configure(qbit_allow_empty=True)
    lib.show("别的番").season(1).local("别的番 S01E01.mkv")
    lib.bangumi(id=37, official_title=TITLE, title_raw="Hyakkano", season=3, episode_offset=-24,
                save_path=str(lib.path(TITLE) / "Season 3"))

    [f] = _proposals(lib)

    assert f.action.op == "create_show_dir"
    assert f.action.args["intent"]["episode_offsets"] == {"3": -24}
    assert not lib.path(TITLE).exists()


def test_dry_run_and_diagnose_do_not_write(lib):
    sh = _scene(lib, sidecar={})
    before = sc_mod.path_for(sh.path).read_bytes()

    lib.diagnose(detectors=[AbAdoptionDetector])
    c = lib.cycle(dry_run=True, detectors=[AbAdoptionDetector])

    assert [r["reason"] for r in c.skipped("adopt_episode_offset")] == ["dry-run"]
    assert sc_mod.path_for(sh.path).read_bytes() == before


def test_a_corrupt_sidecar_is_not_overwritten(lib):
    sh = _scene(lib)
    findings = lib.diagnose(detectors=[AbAdoptionDetector])
    sc_mod.path_for(sh.path).write_text("{ broken", encoding="utf-8")

    rep = lib.apply(findings)

    [rec] = rep.skipped
    assert "解析不了" in rec["reason"]
    assert sc_mod.path_for(sh.path).read_text(encoding="utf-8") == "{ broken"


def test_rollback_removes_only_the_adopted_entry(lib):
    sh = _scene(lib, sidecar={"episode_offsets": {"1": 3}})
    c = lib.cycle(detectors=[AbAdoptionDetector])
    sh.sidecar(notes=["回退之前人又写了一句"])

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    sc = lib.sidecar(TITLE)
    assert sc.episode_offsets == {"1": 3} and sc.notes == ["回退之前人又写了一句"]


def test_rollback_leaves_a_later_human_edit_alone(lib):
    sh = _scene(lib)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    sh.sidecar(episode_offsets={"3": -23})

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "-23" in res["skipped_detail"][0]["skip_reason"]
    assert lib.sidecar(TITLE).episode_offsets == {"3": -23}


def test_after_the_migration_the_offset_survives_disabling_the_ab_row(lib):
    """端到端：迁移之后在 AB 里停用订阅（`deleted=1`，media-agent 读不到那一行了），`- 25` 仍是 S03E01。"""
    sh = _scene(lib)
    sh.season(3).single(RAW_25)
    lib.cycle(detectors=[AbAdoptionDetector])
    conn = sqlite3.connect(lib.abdb.db_path)
    conn.execute("UPDATE bangumi SET deleted=1 WHERE id=37")
    conn.commit()
    conn.close()

    state = lib.scan(resolve_tmdb=False)
    [show] = state.shows
    [f] = [f for f in show.files if f.filename == RAW_25]

    assert show.bangumi is None
    assert _resolve(f, show) == (3, 1)
