"""TMDB 身份钉住与标题稳定闸（critic N4、LAT-04）。

**现场**：
- LAT-04（《鬼物语》）：`鬼物语/Season 1/` 的文件 2026-08-20 按 TMDB 46195 改成 `物语系列 S01E0x`；09-19 10:49
  30 天的缓存过期、按目录名重新搜没搜到，标题退回目录名，文件改回 `鬼物语 …`；16:51 又搜到，再改成 `物语系列 …`。
  09-16 另有 19 个 `终物语 下` 与 6 个 `续・终物语` 的文件在缓存过期、重新搜索选中了另一个条目之后被改名。
- critic N4：扫描从不读 sidecar 里的 `tmdb_id`，缓存按目录名存——目录一改名就换了键、重新搜；多个候选时还会问模型，
  而模型的选择每轮重新做、谁也看不见，直接决定改名目标、目录名、分类。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

import pytest
from harness import weekly

from media_agent import cli, titles
from media_agent import sidecar as sc_mod
from media_agent.cache import Cache

ID = 1201                                 # 合成的；生产上是物语系列的条目


def _age_cache(lib, key: str, days: float) -> None:
    c = Cache(lib.cfg.cache_db)
    c.conn.execute("UPDATE tmdb SET ts = ts - ? WHERE key = ?", (days * 86400, key))
    c.conn.commit()


def _run(lib):
    """`run` 的那一部分：一轮 扫描 → 诊断 → 执行，然后把这一轮的标题决定记下来（`cmd_run` 同样这么做）。"""
    c = lib.cycle()
    assert titles.record(lib.cfg.state_dir, c.state.title_decisions, run_id=c.run_id) == ""
    return c


def _renamed_to(c) -> list[str]:
    return sorted(r["args"]["new_name"] if r["op"] == "rename_show_dir" else
                  r["args"].get("new_name") or r["args"].get("target", "")
                  for r in c.report.applied if r["op"] in ("rename", "rename_show_dir"))


def _search_calls(lib) -> list:
    return [c for c in lib.tmdb.calls if c[0] == "search_tv"]


# ------------------------------------------------------------------ LAT-04
def _monogatari(lib, title: str = "物语系列"):
    """物语系列：TMDB 把多部作品收成一个条目，库里按作品分目录——两个目录都钉在这一个条目上。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(ID, title, seasons={1: weekly(10, first_days_ago=4000)})
    host = lib.show("化物语")
    for n in range(1, 11):
        host.season(1).local(f"物语系列 S01E{n:02d}.mkv")
    host.sidecar(tmdb_id=ID, tmdb_title="物语系列")
    oni = lib.show("鬼物语")
    for n in (1, 2):
        oni.season(1).local(f"物语系列 S01E{n:02d}.mkv")
    oni.sidecar(tmdb_id=ID, tmdb_title="物语系列")
    return oni


@pytest.mark.allow("tmdb_unknown", "log_failure")
def test_lat04_unresolved_tmdb_never_renames_back_to_the_dir_name(lib):
    oni = _monogatari(lib)
    first = _run(lib)
    assert not first.actions("rename")

    # 30 天后缓存过期，这一轮 TMDB 取不到这个条目（2026-09-19 10:49 那一轮）
    _age_cache(lib, f"tmdbshow:{ID}", 31)
    lib.tmdb._shows.pop(ID)
    c = _run(lib)

    assert not c.actions("rename") and not c.actions("rename_show_dir")
    assert sorted(p.name for p in (oni.path / "Season 1").iterdir()) == [
        "物语系列 S01E01.mkv", "物语系列 S01E02.mkv"]
    assert c.state.title_decisions[ID].status == "unresolved"
    assert {s.tmdb_title for s in c.state.shows} == {"物语系列"}


def test_pinned_tmdb_id_is_used_without_searching(lib):
    _monogatari(lib)

    c = lib.cycle()

    assert _search_calls(lib) == []                             # 身份在 sidecar 里：不搜
    assert {s.dir_name: s.tmdb_id for s in c.state.shows} == {"化物语": ID, "鬼物语": ID}
    assert [x for x in lib.tmdb.calls if x[0] == "tv_detail"] == [("tv_detail", ID)]   # 按 id 缓存：两个目录一次


def test_tmdb_cache_is_keyed_by_id_so_a_dir_rename_does_not_re_resolve(lib):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(7, "朱音落语", seasons={1: weekly(3, first_days_ago=900)})
    sh = lib.show("Akane-banashi")
    sh.season(1).local("朱音落语 S01E01.mkv")
    sh.sidecar(tmdb_id=7, tmdb_title="朱音落语")

    first = lib.cycle()
    assert first.applied("rename_show_dir")                    # title-drift：目录名跟 TMDB 标题
    calls = len(lib.tmdb.calls)
    again = lib.cycle()

    assert [s.dir_name for s in again.state.shows] == ["朱音落语"]
    assert again.state.shows[0].tmdb_id == 7
    assert lib.tmdb.calls[calls:] == []                        # 换了目录名也不重新查


def test_legacy_dir_name_cache_entry_is_reused_for_the_pinned_id(lib):
    """部署后的第一轮：旧的按目录名缓存的条目 id 对得上，就不打 TMDB。"""
    _monogatari(lib)
    Cache(lib.cfg.cache_db).put_tmdb("鬼物语", {"id": ID, "title": "物语系列", "seasons": []})
    Cache(lib.cfg.cache_db).put_tmdb("化物语", {"id": ID, "title": "物语系列", "seasons": []})

    lib.cycle()

    assert lib.tmdb.calls == [] or all(c[0] == "season_episodes" for c in lib.tmdb.calls)


# ------------------------------------------------------------------ 旧版记下的 tmdb_id：要有佐证才钉住
KIMYO = 71488                              # 生产上《世界奇妙物语》（TV 剧集）的条目
KIMYO_SP = "世界奇妙物语 2018春之特别篇 (2018)"


def _kimyo(lib, **sidecar):
    """2026-09-27 生产快照：`世界奇妙物语/` 08-17 写的 sidecar（tmdb_id 71488）跟着目录在 media-agent 之外改了名
    （审计里没有 rename_show_dir；09-15 sidecar-sync 只记了「规范名」）。v0.4.1 按目录名搜不到、这个目录名没有缓存，
    从不动它；照旧的 tmdb_id 认，title-drift 要把目录改回 `世界奇妙物语`、missing-nfo 要写剧集的 tvshow.nfo。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(KIMYO, "世界奇妙物语", seasons={1: weekly(3, first_days_ago=9000)})
    sh = lib.show(KIMYO_SP)
    sh.folder("").local(f"{KIMYO_SP}.mkv", size=2_000_000_000)
    sh.sidecar(canonical_title=KIMYO_SP, tmdb_id=KIMYO, tmdb_title="世界奇妙物语", **sidecar)
    return sh


def test_a_legacy_id_nothing_vouches_for_is_held_not_acted_on(lib):
    _kimyo(lib)

    c = lib.cycle(dry_run=True)

    assert not c.actions("rename_show_dir") and not c.actions("write_nfo") and not c.actions("rename")
    assert c.state.shows[0].tmdb_id is None                     # 这一轮不认这个身份（与 v0.4.1 一样：没有身份）
    [f] = [f for f in c.findings if f.kind == "naming_held"]
    assert str(KIMYO) in f.summary and "tmdb_source" in f.summary
    assert f.severity == "important"


@pytest.mark.parametrize("vouch", ["human", "pinned-title", "legacy-cache"])
def test_a_legacy_id_someone_vouches_for_is_pinned(lib, vouch):
    """人写了 `tmdb_source`（或钉了标题），或者 v0.4.1 按这个目录名缓存的就是这个条目：照它认，不搜。"""
    if vouch == "human":
        _kimyo(lib, tmdb_source="human")
    elif vouch == "pinned-title":
        _kimyo(lib, pinned=["tmdb_title"])
    else:
        _kimyo(lib)
        Cache(lib.cfg.cache_db).put_tmdb(KIMYO_SP, {"id": KIMYO, "title": "世界奇妙物语", "seasons": []})

    c = lib.cycle(dry_run=True)

    assert c.state.shows[0].tmdb_id == KIMYO and not c.state.shows[0].naming_hold
    assert _search_calls(lib) == []


# ------------------------------------------------------------------ 负缓存
def test_a_failed_search_is_negative_cached(lib):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.enabled = True
    lib.show("没有条目的番").season(1).local("没有条目的番 S01E01.mkv")

    lib.cycle()
    assert _search_calls(lib) == [("search_tv", "没有条目的番")]
    lib.cycle()
    assert len(_search_calls(lib)) == 1                         # TTL 内不再搜

    Cache(lib.cfg.cache_db).conn.execute("UPDATE tmdb SET ts = ts - 2 * 86400").connection.commit()
    lib.cycle()
    assert len(_search_calls(lib)) == 2                         # 过期了再试


# ------------------------------------------------------------------ 标题变了：连看两轮才采用
def _kusuriya(lib, title: str = "药屋少女的呢喃"):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(1, title, seasons={1: weekly(3, first_days_ago=900)})
    sh = lib.show("药屋少女的呢喃")
    for n in (1, 2, 3):
        sh.season(1).local(f"药屋少女的呢喃 S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=1, tmdb_title="药屋少女的呢喃")
    return sh


def _retitle(lib, tid: int, title: str) -> None:
    lib.tmdb._shows[tid]["name"] = lib.tmdb._shows[tid]["original_name"] = title
    _age_cache(lib, f"tmdbshow:{tid}", 31)


def test_a_new_title_is_adopted_only_after_two_consecutive_runs(lib):
    _kusuriya(lib)
    assert not _renamed_to(_run(lib))

    _retitle(lib, 1, "药屋少女的独语")
    second = _run(lib)
    assert not _renamed_to(second)                              # 第一次看到：还用旧的
    assert second.state.title_decisions[1].status == "pending"
    assert lib.sidecar("药屋少女的呢喃").tmdb_title == "药屋少女的呢喃"   # 档案里也还是旧的
    [held] = [f for f in second.findings if f.kind == "tmdb_title_pending"]
    assert "药屋少女的独语" in held.summary

    third = _run(lib)
    assert third.state.title_decisions[1].status == "adopted"
    assert "药屋少女的独语" in _renamed_to(third)               # 连续第二轮：采用，目录名 / 文件名跟上


def test_a_gap_restarts_the_count(lib):
    _kusuriya(lib)
    _run(lib)
    _retitle(lib, 1, "药屋少女的独语")
    _run(lib)                                                   # 看到 1 轮
    _retitle(lib, 1, "药屋少女的呢喃")
    _run(lib)                                                   # 又变回去（= 已采用的）：不算
    _retitle(lib, 1, "药屋少女的独语")

    c = _run(lib)

    assert c.state.title_decisions[1].status == "pending" and c.state.title_decisions[1].runs == 1
    assert not _renamed_to(c)


@pytest.mark.allow("log_failure", match="TMDB")
def test_a_run_where_tmdb_could_not_be_read_breaks_the_streak(lib):
    """"连续两轮"：中间一轮 TMDB 取不到（unresolved）也算断了——重新从第 1 轮数（`pending.seq` 对不上 `Book.seq`）。
    `test_a_gap_restarts_the_count` 的中间一轮给回了已采用的标题，pending 自己就清掉了，`seq` 的检查从没被用到
    （2026-09-27 审查：去掉它的变异全套存活）。"""
    import httpx
    _kusuriya(lib)
    _run(lib)
    _retitle(lib, 1, "药屋少女的独语")
    assert _run(lib).state.title_decisions[1].status == "pending"         # 看到 1 轮
    real = lib.tmdb.tv_detail

    def down(tv_id):
        raise httpx.ConnectTimeout("connect timed out")

    lib.tmdb.tv_detail = down
    _age_cache(lib, "tmdbshow:1", 31)
    assert _run(lib).state.title_decisions[1].status == "unresolved"      # 这一轮取不到：断了
    lib.tmdb.tv_detail = real

    c = _run(lib)

    d = c.state.title_decisions[1]
    assert (d.status, d.runs) == ("pending", 1)
    assert not _renamed_to(c)


def test_one_tmdb_search_error_stops_searching_for_the_rest_of_the_scan(lib):
    """扫描的断路器：一轮里 TMDB 出过一次错，其余没钉住的番不再搜、不再取详情（用缓存兜底）。以前只有 `tv_detail`
    那条路有测试，`search_tv` 出错不设断路器的变异全套存活（2026-09-27 审查）。"""
    import httpx
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.enabled = True
    for name in ("甲番", "乙番", "丙番"):
        lib.show(name).season(1).local(f"{name} S01E01.mkv")
    calls = []

    def down(q):
        calls.append(q)
        raise httpx.ConnectTimeout("connect timed out")

    lib.tmdb.search_tv = down
    lib.tripwire.allow("log_failure", match="TMDB 查询失败")

    lib.scan()

    assert len(calls) == 1
    assert [c for c in lib.tmdb.calls if c[0] in ("search_tv", "tv_detail")] == []


def test_a_title_does_not_flip_back_within_30_days(lib):
    sh = _kusuriya(lib)
    _run(lib)
    _retitle(lib, 1, "药屋少女的独语")
    _run(lib)
    adopted = _run(lib)
    assert adopted.state.title_decisions[1].status == "adopted"
    new_dir = lib.media_root / "药屋少女的独语"
    assert new_dir.is_dir() and not sh.path.exists()

    _retitle(lib, 1, "药屋少女的呢喃")                           # TMDB 又给回刚换掉的那个
    for _ in range(3):
        c = _run(lib)
        assert c.state.title_decisions[1].status == "flip_blocked"
        assert not _renamed_to(c)
    [f] = [f for f in c.findings if f.kind == "tmdb_title_flip_blocked"]
    assert f.severity == "important" and "药屋少女的呢喃" in f.summary
    assert lib.sidecar("药屋少女的独语").tmdb_title == "药屋少女的独语"   # 档案记的是采用的，不是 TMDB 这一轮给的

    # 31 天之后：按普通的改标题处理（仍要连看两轮）
    book = json.loads(titles.path_of(lib.cfg.state_dir).read_text(encoding="utf-8"))
    for e in book["shows"].values():
        for x in e.get("left", []):
            x["at"] = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
    titles.path_of(lib.cfg.state_dir).write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
    _age_cache(lib, "tmdbshow:1", 31)
    assert _run(lib).state.title_decisions[1].status == "pending"
    assert _run(lib).state.title_decisions[1].status == "adopted"


def test_a_human_pinned_title_wins(lib):
    sh = _kusuriya(lib, title="The Apothecary Diaries")
    sh.sidecar(pinned=["tmdb_title"])

    c = _run(lib)

    assert c.state.shows[0].tmdb_title == "药屋少女的呢喃"
    assert 1 not in c.state.title_decisions                     # 不过稳定闸、不记
    assert not _renamed_to(c)


def test_diagnose_does_not_advance_the_title_count(lib, monkeypatch):
    """"连续两轮"数的是 `run`：人手跑几次 `diagnose` 不能把一个新标题"确认"下来。"""
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    _kusuriya(lib)
    _run(lib)
    _retitle(lib, 1, "药屋少女的独语")
    args = argparse.Namespace(no_tmdb=False, json=False)
    for _ in range(3):
        assert cli.cmd_diagnose(args, lib.cfg) == 0

    assert lib.cycle().state.title_decisions[1].status == "pending"


def test_cmd_run_records_the_title_decisions(lib, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    _kusuriya(lib)
    args = argparse.Namespace(dry_run=False, no_tmdb=False, no_evolve=True, max_proposals=0,
                              json=False)

    cli.cmd_run(args, lib.cfg)

    book, problem = titles.load(lib.cfg.state_dir)
    assert problem == "" and book.seq == 1
    assert book.entry(1)["adopted"] == "药屋少女的呢喃"


# ------------------------------------------------------------------ 部署后第一轮：以库里此刻在用的名字为已采用
PUNCH = 244617                             # 生产上《深夜重拳》的条目


@pytest.mark.parametrize("legacy_cache", [True, False], ids=["legacy-cache", "no-cache"])
def test_first_run_after_deploy_keeps_the_name_the_library_already_uses(lib, legacy_cache):
    """2026-09-27 生产快照复现：`深夜重拳/` 与文件 09-16 已随 TMDB 改名（`rename_show_dir 深夜Punch -> 深夜重拳`），
    sidecar 的 `tmdb_title` 却还是第一次写时的「深夜Punch」（e47a054 之前 sidecar-sync 只在 tmdb_id 变了时重写它）。
    以它为"已采用"，部署后第一轮把库改回「深夜Punch」、第二轮确认新标题再改回来——LAT-04 的来回改名，由部署触发。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(PUNCH, "深夜重拳", seasons={1: weekly(2, first_days_ago=400)})
    sh = lib.show("深夜重拳")
    for n in (1, 2):
        sh.season(1).local(f"深夜重拳 S01E{n:02d}.mp4")
    sh.sidecar(tmdb_id=PUNCH, tmdb_title="深夜Punch")
    if legacy_cache:                                            # v0.4.1 按目录名缓存、按它命名的条目
        Cache(lib.cfg.cache_db).put_tmdb("深夜重拳", {"id": PUNCH, "title": "深夜重拳", "seasons": [
            {"season_number": 1, "episode_count": 2, "name": "第 1 季"}]})

    first = _run(lib)
    second = _run(lib)

    assert first.state.title_decisions[PUNCH].status == "stable"
    assert not _renamed_to(first) and not _renamed_to(second)
    assert sorted(p.name for p in (sh.path / "Season 1").iterdir()) == [
        "深夜重拳 S01E01.mp4", "深夜重拳 S01E02.mp4"]
    assert lib.sidecar("深夜重拳").tmdb_title == "深夜重拳"      # 档案跟上此刻在用的


def test_first_run_after_deploy_still_confirms_a_title_the_library_does_not_use_yet(lib):
    """对照：库里（目录、文件）用的是旧标题、TMDB 这一轮给了新的——仍要连看两轮才改，部署不让它直接生效。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(PUNCH, "深夜重拳", seasons={1: weekly(2, first_days_ago=400)})
    sh = lib.show("深夜Punch")
    for n in (1, 2):
        sh.season(1).local(f"深夜Punch S01E{n:02d}.mp4")
    sh.sidecar(tmdb_id=PUNCH, tmdb_title="深夜Punch")

    first = _run(lib)
    assert first.state.title_decisions[PUNCH].status == "pending"
    assert not _renamed_to(first)
    second = _run(lib)
    assert second.state.title_decisions[PUNCH].status == "adopted"
    assert "深夜重拳" in _renamed_to(second)


@pytest.mark.allow("tmdb_unknown", "log_failure")
def test_no_known_title_at_all_holds_naming_for_that_show(lib):
    """人钉了 tmdb_id、sidecar 里没有标题、TMDB 这一轮也取不到：不按目录名 / AB 标题去改任何名字。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.enabled = True
    sh = lib.show("药屋少女的呢喃")
    sh.season(1).single("[LoliHouse] Kusuriya no Hitorigoto - 01 [WebRip 1080p][简繁内封字幕].mkv")
    sh.sidecar(tmdb_id=999)

    c = lib.cycle()

    assert not c.actions("rename") and not c.actions("recategorize")
    [f] = [f for f in c.findings if f.kind == "naming_held"]
    assert "999" in f.summary


@pytest.mark.allow("tmdb_unknown", "log_failure")
@pytest.mark.parametrize("where", ["tv_detail", "search_tv"])
def test_a_tmdb_http_error_never_puts_the_api_key_into_findings_or_logs(lib, where):
    """2026-09-27 审查：扫描的断路器原因是 `f"{type(e).__name__}: {e}"`——httpx 的 HTTPStatusError 文本带着整个请求 URL，
    `api_key=` 就在里面。它进了 `naming_hold`（→ `naming_held` 发现的摘要与 evidence → 发现历史）、`ctx.tmdb_scan_down`、
    run.err.log。v0.4.1 为同一件事（run.log 里的 api_key 明文）专门出过一版。"""
    from test_episode_cache import _http_error

    lib.configure(qbit_allow_empty=True)
    lib.tmdb.enabled = True
    sh = lib.show("药屋少女的呢喃")
    sh.season(1).single("[LoliHouse] Kusuriya no Hitorigoto - 01 [WebRip 1080p][简繁内封字幕].mkv")
    if where == "tv_detail":
        sh.sidecar(tmdb_id=999, tmdb_source="human")

        def boom(tv_id):
            raise _http_error(503)
        lib.tmdb.tv_detail = boom
    else:
        def boom(q):
            raise _http_error(429)
        lib.tmdb.search_tv = boom

    c = lib.cycle(dry_run=True)

    leaked = [m for m in lib.logs if "SECRETKEY123" in m]
    leaked += [f.summary for f in c.findings if "SECRETKEY123" in f.summary]
    leaked += [str(f.evidence) for f in c.findings if "SECRETKEY123" in str(f.evidence)]
    leaked += [s.naming_hold for s in c.state.shows if "SECRETKEY123" in s.naming_hold]
    assert "SECRETKEY123" not in str(getattr(c.ctx, "tmdb_scan_down", ""))
    assert leaked == []
    assert any("HTTP 5" in m or "HTTP 4" in m for m in lib.logs)   # 状态码照样说出来


def _held_scene(lib):
    """一部各个按标题的规则都有事做的番：目录名不是 TMDB 标题（title-drift）、没有 NFO（missing-nfo）、一个还叫发布名的
    种子（unrenamed-file），它的分类也不是标题（category-consolidation），第 9 集已播、番组页上有（episode-available）。"""
    from harness import MikanItem
    lib.configure(qbit_allow_empty=True)
    schedule = weekly(12, first_days_ago=60)
    lib.tmdb.add_show(1234, "尼古喵喵", seasons={1: schedule})
    sh = lib.show("Yani-Neko")
    s1 = sh.season(1)
    for n in range(1, 8):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    s1.single("[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC].mkv", category="Yani Neko")
    sh.sidecar(tmdb_id=1234, tmdb_title="尼古喵喵", tmdb_source="human", mikan_id="3500",
               seasons={"1": {"have": list(range(1, 9))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    lib.mikan("3500", [MikanItem(title=title, pub=dict(schedule)[9])], search=["尼古喵喵", "Yani-Neko"])


def _detectors_by_op():
    from media_agent.plugins.builtin import (CategoryConsolidationDetector, MissingNfoDetector,
                                             TitleDriftDetector, UnrenamedDetector)
    from media_agent.plugins.grab import EpisodeAvailableDetector
    return [(EpisodeAvailableDetector, "grab_episode"), (CategoryConsolidationDetector, "recategorize"),
            (UnrenamedDetector, "rename"), (TitleDriftDetector, "rename_show_dir"),
            (MissingNfoDetector, "write_nfo")]


@pytest.mark.parametrize("detector, op", _detectors_by_op(), ids=[op for _d, op in _detectors_by_op()])
def test_every_title_based_rule_honours_naming_hold(lib, detector, op):
    """`naming_held` 的摘要承诺这一轮不改名、目录名、分类、NFO、抓取。以前只有改名在这里测过（再加上 converge 那条
    刚钉住的测试顺带测了目录名与 NFO）：抓取与分类合并去掉 hold 检查的变异全套存活（2026-09-27 审查）。
    同一份扫描：没有 hold 时这条规则提议这个动作，挂上 hold 就不提。"""
    _held_scene(lib)
    state = lib.scan()
    assert op in {f.action.op for f in lib.diagnose(state, detectors=[detector]) if f.action}

    for show in state.shows:
        show.naming_hold = "测试：标题认不准"

    assert op not in {f.action.op for f in lib.diagnose(state, detectors=[detector]) if f.action}


def test_corrupt_sidecar_holds_naming(lib):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.enabled = True
    sh = lib.show("药屋少女的呢喃")
    sh.season(1).single("[LoliHouse] Kusuriya no Hitorigoto - 01 [WebRip 1080p][简繁内封字幕].mkv")
    sc_mod.path_for(sh.path).write_text('{"tmdb_id": 1,', encoding="utf-8")

    c = lib.cycle()

    assert not c.actions("rename")
    assert _search_calls(lib) == []                             # 身份认不准：不去搜一个可能不同的条目


# ------------------------------------------------------------------ 模型选的条目：钉进 sidecar
def _ambiguous(lib):
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(11, "葬送的芙莉莲", queries=["Frieren"], first_air_date="2023-09-29",
                      seasons={1: weekly(3, first_days_ago=900)})
    lib.tmdb.add_show(12, "葬送的芙莉莲 迷你剧场", queries=["Frieren"], first_air_date="2023-10-06")
    sh = lib.show("Frieren")
    sh.season(1).local("Frieren S01E01.mkv")
    return sh


def test_llm_pick_is_proposed_as_a_pin_and_not_used_before_it_is_pinned(lib):
    sh = _ambiguous(lib)
    lib.llm.when(lambda s, u: "Frieren" in u, {"id": 11, "confidence": 0.9, "reason": "正篇"})

    c = lib.cycle()

    assert len(lib.llm.prompts) == 1
    [f] = [f for f in c.findings if f.kind == "tmdb_pick"]
    assert f.action.op == "pin_tmdb" and f.action.args["tmdb_id"] == 11
    assert c.state.shows[0].tmdb_id is None                     # 这一轮不按模型的选择改任何名字
    assert not c.actions("rename_show_dir")
    [rec] = c.applied("pin_tmdb")
    assert rec["undo"]["op"] == "restore_sidecar"
    raw = json.loads(sc_mod.path_for(sh.path).read_text(encoding="utf-8"))
    assert (raw["tmdb_id"], raw["tmdb_source"]) == (11, "llm")

    again = lib.cycle()
    assert len(lib.llm.prompts) == 1                            # 钉住了：不再问、不再搜
    assert again.state.shows[0].tmdb_id == 11


def test_llm_pick_is_not_re_asked_every_dry_run(lib):
    _ambiguous(lib)
    lib.llm.when(lambda s, u: "Frieren" in u, {"id": 11, "confidence": 0.9, "reason": "正篇"})

    lib.cycle(dry_run=True)
    c = lib.cycle(dry_run=True)

    assert len(lib.llm.prompts) == 1
    assert [f.action.args["tmdb_id"] for f in c.findings if f.kind == "tmdb_pick"] == [11]
    # 缓存里的答案同样只是提议：钉进 sidecar 之前不按它认、不按它改任何名字（critic N4）。`pin_tmdb` 回退之后、或它一直
    # 写不进去的时候，每一轮走的都是这条缓存路径（2026-09-27 审查：变异"缓存命中就用它"全套存活）
    assert c.state.shows[0].tmdb_id is None
    assert not {"rename", "rename_show_dir", "write_nfo", "recategorize"} & {f.action.op for f in c.findings
                                                                            if f.action}


def test_llm_is_never_asked_for_a_pinned_show(lib):
    sh = _ambiguous(lib)
    sh.sidecar(tmdb_id=12, tmdb_title="葬送的芙莉莲 迷你剧场", tmdb_source="human")
    lib.tmdb._shows[12]["seasons"] = {1: weekly(3, first_days_ago=900)}
    lib.llm.when(lambda s, u: True, {"id": 11, "confidence": 0.99, "reason": "x"})

    c = lib.cycle()

    assert lib.llm.prompts == []
    assert c.state.shows[0].tmdb_id == 12


def test_pin_tmdb_never_overwrites_an_existing_id(lib):
    sh = _ambiguous(lib)
    lib.llm.when(lambda s, u: "Frieren" in u, {"id": 11, "confidence": 0.9, "reason": "正篇"})
    [f] = [f for f in lib.diagnose() if f.kind == "tmdb_pick"]
    sh.sidecar(tmdb_id=12)                                      # 诊断之后人钉了另一个

    rep = lib.apply([f])

    [rec] = rep.skipped
    assert "12" in rec["reason"]
    assert json.loads(sc_mod.path_for(sh.path).read_text(encoding="utf-8"))["tmdb_id"] == 12


# ------------------------------------------------------------------ 稳定闸本身
def test_book_decisions_unit():
    b = titles.Book()
    assert b.decide(1, "A").status == "first"
    assert b.decide(1, "A", fallback="A").status == "stable"
    d = b.decide(1, "B", fallback="A")
    assert (d.status, d.title, d.runs) == ("pending", "A", 1)
    titles.apply(b, {1: d})
    d2 = b.decide(1, "B")
    assert (d2.status, d2.title, d2.runs) == ("adopted", "B", 2)
    titles.apply(b, {1: d2})
    d3 = b.decide(1, "A")
    assert (d3.status, d3.title) == ("flip_blocked", "B")
    assert b.decide(1, None).status == "unresolved" and b.decide(1, None).title == "B"


def test_unreadable_book_is_reported_and_treated_as_empty(tmp_path):
    titles.path_of(tmp_path).write_text("{坏", encoding="utf-8")
    book, problem = titles.load(tmp_path)
    assert problem and book.seq == 0
    assert "没有记标题" in titles.record(tmp_path, {1: titles.Decision(1, "A", "first", "A")})
