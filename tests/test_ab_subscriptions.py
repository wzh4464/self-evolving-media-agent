"""AutoBangumi 的每一条有效订阅，media-agent 都能开始抓——哪怕一集都还没下（`ab-adoption` 的订阅接手）。

AB 仍是订阅的前端（用户在 AB 的 WebUI 里订，`autobangumi-subscribe-verify` 流程）。可 media-agent 以前要等 AB 把第一个文件
放进番目录：扫描只登记有文件的目录、抓取只看盘上有文件的季（`test_subscriptions.py` 的模块文档）。AB 的下载一关，新订阅
就一集都不会来。

现在对每一条 `deleted=0` 的订阅行：
- 番目录还没有：`create_show_dir`——建 `<媒体根>/<番名>/`、各订阅季的 `Season N/`、一份只有人的意图的 sidecar
  （`subscriptions`、订阅 `rss_link` 里的番组页 id、AB 的集号偏移）。**这是这一步唯一的媒体根写入**，有逆操作
  （`remove_show_dir`：目录里只剩它自己建的东西时才删）。TMDB 身份照旧由扫描搜、sidecar-sync / `pin_tmdb` 钉。
- 番目录在、sidecar 的 `seasons` 与 `subscriptions` 都还没有这一季（盘上有了、还没记进 `seasons` 的也算）：`subscribe_season`。
  `seasons` 里已经有这一季的
  订阅不写——抓取本来就看它，生产上 35 条订阅不因此多出 35 次写。
- 订阅行认不出番目录（`save_path` 不在媒体根下）：报出来，不动手。
AB 库用真的 sqlite（`lib.bangumi`）；只读。
"""
from __future__ import annotations

import json

import pytest
from harness import MikanItem, video, weekly

from media_agent import sidecar as sc_mod
from media_agent.plugins.adopt import AbAdoptionDetector
from media_agent.plugins.grab import EpisodeAvailableDetector

NEW = "新番乙"
TMDB = 3401
MID = "4601"
RSS = f"https://mikanani.me/RSS/Bangumi?bangumiId={MID}&subgroupid=583"
TPL = "[LoliHouse] 新番乙 / Shinban Otsu - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _row(lib, *, id=51, name=NEW, season=1, rss=RSS, **kw):
    return lib.bangumi(id=id, official_title=name, title_raw="Shinban Otsu", season=season,
                       rss_link=rss, save_path=str(lib.media_root / name / f"Season {season}"), **kw)


def _airing(lib, *, season=1, first_days_ago=20, eps=(1, 2, 3)):
    sched = weekly(12, first_days_ago=first_days_ago)
    lib.tmdb.add_show(TMDB, NEW, seasons={season: sched})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in eps], search=[NEW])


def _findings(lib):
    return lib.diagnose(detectors=[AbAdoptionDetector])


def test_a_new_subscription_gets_a_dir_and_starts_grabbing_in_the_same_run(lib):
    """AB 刚订的新番（AB 一集都还没下）：同一轮 `run` 里建目录、登记订阅，下一次迭代扫描认得它、按番组页抓。"""
    _row(lib)
    _airing(lib)

    loop = lib.loop(detectors=[AbAdoptionDetector, EpisodeAvailableDetector])

    [made] = loop.applied("create_show_dir")
    d = lib.path(NEW)
    assert d.is_dir() and (d / "Season 1").is_dir()
    sc = json.loads(sc_mod.path_for(d).read_text(encoding="utf-8"))
    assert sc["subscriptions"] == {"1": {"source": "autobangumi", "bangumi_id": 51, "mikan_id": MID}}
    assert sc["mikan_id"] == MID and sc["canonical_title"] == NEW
    assert "tmdb_id" not in sc or sc["tmdb_id"] is None           # 身份由扫描搜、sidecar-sync 钉，不由这一步写
    assert made["undo"] == {"op": "remove_show_dir", "path": str(d), "created": ["Season 1"]}
    grabs = loop.applied("grab_episode")
    assert sorted(r["args"]["episode"] for r in grabs) == [1, 2, 3]
    assert {r["save_path"] for r in grabs} == {str(d / "Season 1")}


def test_two_rows_for_one_missing_show_make_one_dir(lib):
    _row(lib, id=51, season=1)
    _row(lib, id=52, season=2, rss="https://mikanani.me/RSS/Bangumi?bangumiId=4602&subgroupid=1")

    [f] = [f for f in _findings(lib) if f.action]

    assert f.action.op == "create_show_dir" and f.kind == "ab_subscription_new"
    assert f.action.args["seasons"] == [1, 2]
    assert f.action.args["intent"]["subscriptions"] == {
        "1": {"source": "autobangumi", "bangumi_id": 51, "mikan_id": MID},
        "2": {"source": "autobangumi", "bangumi_id": 52, "mikan_id": "4602"}}


def test_the_ab_offset_rides_along_when_the_dir_is_created(lib):
    _row(lib, season=3, episode_offset=-24)

    rep = lib.apply(_findings(lib))

    [rec] = rep.applied
    assert lib.sidecar(NEW).episode_offsets == {"3": -24}
    assert not [f for f in _findings(lib) if f.action]             # 偏移已经在了：不再单独迁
    assert rec["op"] == "create_show_dir"


def test_a_search_style_rss_link_gives_no_page_id(lib):
    _row(lib, rss="https://mikanani.me/RSS/Search?searchstr=Shinban%20Otsu")

    [f] = [f for f in _findings(lib) if f.action]

    assert f.action.args["intent"]["subscriptions"]["1"] == {"source": "autobangumi", "bangumi_id": 51}
    assert "mikan_id" not in f.action.args["intent"]


def test_an_existing_show_gets_the_new_season_registered(lib):
    """老番的新一季刚在 AB 里订上：番目录在、第二季盘上还一集都没有。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}}, notes=["人写的"])
    _row(lib, season=2)

    [f] = [f for f in _findings(lib) if f.action]
    assert (f.action.op, f.kind, f.subject) == ("subscribe_season", "ab_subscription", "S02")
    rep = lib.apply([f])

    [rec] = rep.applied
    sc = lib.sidecar(NEW)
    assert sc.subscriptions == {"2": {"source": "autobangumi", "bangumi_id": 51, "mikan_id": MID}}
    assert sc.mikan_id == MID and sc.notes == ["人写的"]
    assert rec["undo"]["op"] == "unset_sidecar"
    assert {e["field"] for e in rec["undo"]["entries"]} == {"subscriptions", "mikan_id"}
    assert not [f for f in _findings(lib) if f.action]


def test_a_season_already_on_disk_is_not_rewritten(lib):
    """盘上已经有这一季（sidecar 的 seasons 里有）：抓取本来就看它，不为"登记一下"多写一次。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, season=1)

    assert not _findings(lib)


def test_an_existing_page_id_is_kept(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}}, mikan_id="1111")
    _row(lib, season=2)

    lib.apply(_findings(lib))

    sc = lib.sidecar(NEW)
    assert sc.mikan_id == "1111" and sc.subscriptions["2"]["mikan_id"] == MID


def test_an_unmappable_save_path_is_reported_not_acted_on(lib):
    lib.bangumi(id=53, official_title=NEW, title_raw="Shinban Otsu", season=1, rss_link=RSS,
                save_path="/downloads/elsewhere/新番乙/Season 1")

    [f] = _findings(lib)

    assert f.kind == "ab_subscription_unmapped" and not f.action
    assert f.evidence["bangumi_id"] == 53
    assert not lib.path(NEW).exists()


def test_a_save_path_that_is_a_season_dir_right_under_the_root_is_unmapped(lib):
    """`save_path` 是 `<媒体根>/Season 1`：媒体根下那一段是季目录名，不是番目录——报 `ab_subscription_unmapped`，不在媒体根下
    建一个叫 `Season 1` 的"番"（`safe_dir_name` 拒季目录名；审查的变异 T2-11 以前全套测试照样过）。"""
    lib.bangumi(id=54, official_title=NEW, title_raw="Shinban Otsu", season=1, rss_link=RSS,
                save_path=str(lib.media_root / "Season 1"))

    [f] = _findings(lib)

    assert f.kind == "ab_subscription_unmapped" and not f.action
    assert not (lib.media_root / "Season 1").exists()


def test_the_season_dir_in_save_path_decides_the_library_season(lib):
    """AB 行上 `season=2`、`season_offset=0`，可 `save_path` 是 `…/Season 3`：AB 的下载落进 Season 3，订阅也登记第 3 季
    （`abrow.library_season`：`save_path` 的 `Season N` 优先；以前每个现场两者都一致，审查的变异 T2-20 照样过）。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    lib.bangumi(id=55, official_title=NEW, title_raw="Shinban Otsu", season=2, rss_link=RSS,
                save_path=str(lib.path(NEW) / "Season 3"))

    [f] = [f for f in _findings(lib) if f.action]

    assert f.action.op == "subscribe_season" and f.action.args["season"] == 3 and f.subject == "S03"


def test_a_corrupt_sidecar_gets_no_subscription_proposed(lib):
    """番目录的档案坏了：不提议订阅（执行时也会拒，但诊断里就不该出一个做不成的动作——坏档案由 sidecar-sync 报）。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sc_mod.path_for(sh.path).write_text("{ broken", encoding="utf-8")
    _row(lib, season=2)

    assert not [f for f in _findings(lib) if f.action]


def test_a_stale_save_path_does_not_make_an_empty_twin(lib):
    """AB 的保存路径还指着改名之前的目录名（人手改了目录、没同步到 AB），而现在的目录的档案记着这条订阅：
    不建一个只有档案的空壳（抓取会把它当重复目录跳过，标题对齐还要往已有的目录上改），报出来。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show("新番乙（改过名）")
    sh.season(1).local("新番乙（改过名） S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}}, bangumi_id=51)
    _row(lib, id=51, season=1)

    [f] = _findings(lib)

    assert f.kind == "ab_subscription_moved" and not f.action
    assert f.evidence["recorded_in"] == "新番乙（改过名）"
    assert not lib.path(NEW).exists()


def test_a_case_variant_of_an_existing_dir_is_not_created_again(lib):
    """生产卷是大小写不敏感的 APFS：`RE ZERO` 与 `Re Zero` 是同一个目录。AB 这一边的名字也要折叠——以前的现场用的是
    `re zero`，它折叠前后一样，查的那一侧不折叠也照样过（审查的变异 T2-29）。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show("Re Zero")
    sh.season(1).local("Re Zero S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, name="RE ZERO", season=1)

    assert not [f for f in _findings(lib) if f.action and f.action.op == "create_show_dir"]


def test_a_torrent_already_claiming_the_dir_blocks_creation(lib):
    """AB 刚加了种子、还没有元数据（盘上什么都没有）：那个目录归它，media-agent 不抢着建。"""
    _row(lib)
    lib.qbit.seed("e" * 40, name="[LoliHouse] Shinban Otsu - 01", save_path=lib.media_root / NEW / "Season 1",
                  files={"[LoliHouse] Shinban Otsu - 01.mkv": 600_000_000}, progress=0.0,
                  category="Bangumi", tags="ab:51")

    rep = lib.apply(_findings(lib))

    [rec] = rep.skipped
    assert "占" in rec["reason"] and "eeeeeeee" in rec["reason"]
    assert not lib.path(NEW).exists()


def test_diagnose_and_dry_run_create_nothing(lib):
    _row(lib)

    _findings(lib)
    c = lib.cycle(dry_run=True, detectors=[AbAdoptionDetector])

    assert [r["reason"] for r in c.skipped("create_show_dir")] == ["dry-run"]
    assert not lib.path(NEW).exists()


def test_rollback_removes_a_dir_that_holds_only_what_it_created(lib):
    _row(lib, season=1)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    (lib.path(NEW) / ".DS_Store").write_bytes(b"\0")             # Finder 撒的元数据不算内容

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert not lib.path(NEW).exists()


def test_rollback_keeps_a_dir_that_has_content_now(lib):
    _row(lib, season=1)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    (lib.path(NEW) / "Season 1" / f"{NEW} S01E01.mkv").write_bytes(b"x")

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "S01E01" in res["skipped_detail"][0]["skip_reason"]
    assert (lib.path(NEW) / "Season 1" / f"{NEW} S01E01.mkv").exists()


def test_rollback_of_a_subscribed_season_only_unsets_it(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, season=2)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    sh.sidecar(notes=["之后人写的"])

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    sc = lib.sidecar(NEW)
    assert sc.subscriptions == {} and sc.mikan_id == "" and sc.notes == ["之后人写的"]


# ------------------------------------------------------------------ 续作的保存路径与已有的番目录名不同
def test_a_sequel_whose_save_path_names_another_folder_is_grabbed_into_the_host(lib):
    """AB 按自己的标题建保存路径（`旧番庚 第二季/Season 2`），本项目的番目录叫 TMDB 的标题（`旧番庚`）。订阅那一刻 AB 一集都
    没补（开播前订的、或全被过滤）：`create_show_dir` 建出一个只有档案的空壳，扫描认出它与 `旧番庚` 是同一个 TMDB 条目、归为
    重复目录——抓取跳过它，宿主又没有第 2 季的订阅，第二季永远不来（2026-09-27 审查复现：grab → run → grab… 零个
    `grab_episode`，只有一条"抓取只认文件最多的「旧番庚」"）。现在重复目录上的订阅并进宿主：第二季抓进 `旧番庚/Season 2`。"""
    lib.configure(qbit_allow_empty=True, ab_mode="subscription")
    s2 = weekly(12, first_days_ago=10)
    lib.tmdb.add_show(3801, "旧番庚", seasons={1: weekly(12, first_days_ago=400), 2: s2},
                      queries=["旧番庚 第二季"])
    host = lib.show("旧番庚")
    for n in range(1, 13):
        host.season(1).local(f"旧番庚 S01E{n:02d}.mkv")
    host.sidecar(tmdb_id=3801, tmdb_source="human", tmdb_title="旧番庚", seasons={"1": {"have": list(range(1, 13))}})
    lib.bangumi(id=81, official_title="旧番庚 第二季", title_raw="Jiufan Geng S2", season=2,
                rss_link="https://mikanani.me/RSS/Bangumi?bangumiId=5081&subgroupid=1",
                save_path=str(lib.media_root / "旧番庚 第二季" / "Season 2"))
    tpl = "[LoliHouse] 旧番庚 第二季 / Jiufan Geng S2 - {:02d} [WebRip 1080p][简繁内封字幕]"
    lib.mikan("5081", [MikanItem(title=tpl.format(n), pub=dict(s2)[n]) for n in (1, 2)],
              search=["旧番庚", "旧番庚 第二季"])

    loop = lib.grab_loop()

    assert loop.applied("create_show_dir")                            # 空壳照建（名字认不出它是谁，交给扫描）
    grabs = loop.applied("grab_episode")
    assert sorted(r["args"]["episode"] for r in grabs) == [1, 2]
    assert {r["save_path"] for r in grabs} == {str(lib.path("旧番庚") / "Season 2")}
    [dup] = [f for f in loop.findings if f.kind == "duplicate_show_dir"]
    assert dup.evidence["folded"] == {"2": "旧番庚 第二季"}


def test_a_new_season_whose_burst_already_landed_is_registered_in_the_same_grab(lib):
    """AB 订阅那一刻补进来的第一集名字里就带着季（`Jiufan Geng S2 - 01`）：以前 `ab-adoption` 认它"盘上已有这一季"、不登记
    订阅，可抓取模式里没有 sidecar-sync——`seasons` 要等 6 小时的 `run` 才记上第 2 季，这之间抓取看不见它，第二集晚半天
    （2026-09-27 审查复现：grab #1 只交接改名，grab #2 什么都不做，run 才抓 S02E02）。sidecar 的 `seasons` 与 `subscriptions`
    都还没有这一季就登记订阅，同一次抓取里就抓。"""
    lib.configure(qbit_allow_empty=True, ab_mode="subscription")
    s2 = weekly(12, first_days_ago=10)
    lib.tmdb.add_show(3801, "旧番庚", seasons={1: weekly(12, first_days_ago=400), 2: s2})
    sh = lib.show("旧番庚")
    for n in range(1, 13):
        sh.season(1).local(f"旧番庚 S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=3801, tmdb_source="human", tmdb_title="旧番庚", seasons={"1": {"have": list(range(1, 13))}})
    lib.bangumi(id=81, official_title="旧番庚", title_raw="Jiufan Geng", season=2,
                rss_link="https://mikanani.me/RSS/Bangumi?bangumiId=5081&subgroupid=1",
                save_path=str(lib.media_root / "旧番庚" / "Season 2"))
    sh.season(2).single("[LoliHouse] Jiufan Geng S2 - 01 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕].mkv",
                        category="Bangumi", probe=video("hevc", subs=["chi 简体中文", "chi 繁體中文"]))
    tpl = "[LoliHouse] 旧番庚 第二季 / Jiufan Geng S2 - {:02d} [WebRip 1080p][简繁内封字幕]"
    lib.mikan("5081", [MikanItem(title=tpl.format(n), pub=dict(s2)[n]) for n in (1, 2)], search=["旧番庚"])

    loop = lib.grab_loop()

    [sub] = loop.applied("subscribe_season")
    assert sub["args"]["season"] == 2
    assert [r["args"]["episode"] for r in loop.applied("grab_episode")] == [2]
    assert lib.sidecar("旧番庚").subscriptions["2"]["mikan_id"] == "5081"


# ------------------------------------------------------------------ 人删掉的番目录不重建
def test_a_show_dir_the_user_deleted_is_not_recreated(lib):
    """AB 里还挂着订阅（播完的番大多如此），人把番目录删了：以前每 30 分钟的抓取都 `create_show_dir` 建回来、接着把整季
    重下一遍（2026-09-27 审查：删掉《杀手青春》之后同一次抓取就提了 6 个 `grab_episode`；35 条有效订阅里 30 条的季还在
    `is_seasonal` 的窗口里）。见过这个目录的订阅（它在过）目录没了就只说一句，不重建。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, season=1)
    assert not [f for f in _findings(lib) if f.action]                 # 目录在：见过了
    import shutil
    shutil.rmtree(lib.path(NEW))

    fs = _findings(lib)

    assert not [f for f in fs if f.action]
    [f] = fs
    assert f.kind == "ab_subscription_dir_gone" and f.severity == "minor" and f.evidence["bangumi_id"] == 51
    assert "停用" in f.summary


def test_a_row_ab_already_downloaded_for_counts_as_seen(lib):
    """部署之前就删了的目录（这个版本从没见过它）：AB 的 torrent 表里有这条订阅下过的种子，说明目录在过——同样不重建。"""
    _row(lib, id=14)
    lib.ab_rows("torrent", [{"id": 1, "bangumi_id": 14, "name": "[G] Shinban Otsu - 01", "url": "magnet:?x",
                             "downloaded": 1}])

    [f] = _findings(lib)

    assert f.kind == "ab_subscription_dir_gone" and not f.action
    assert not lib.path(NEW).exists()


def test_a_rolled_back_dir_is_not_recreated_by_the_next_grab(lib):
    """回退了 `create_show_dir`：下一次诊断不能又把它建回来（那是撤销本轮动作的反向，只是隔了一轮）。"""
    _row(lib, season=1)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    assert c.applied("create_show_dir")
    lib.diagnose(detectors=[AbAdoptionDetector])                      # 下一次抓取看到它在
    assert lib.rollback(c.run_id)["reverted"] == 1

    fs = _findings(lib)

    assert [f.kind for f in fs] == ["ab_subscription_dir_gone"] and not lib.path(NEW).exists()


def test_a_new_row_for_a_deleted_show_still_gets_its_dir(lib):
    """删掉的番又在 AB 里订了新的一季（新的订阅行）：人又要了，照建。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, id=51, season=1)
    _findings(lib)
    import shutil
    shutil.rmtree(lib.path(NEW))
    _row(lib, id=52, season=2, rss="https://mikanani.me/RSS/Bangumi?bangumiId=4602&subgroupid=1")

    fs = _findings(lib)

    [made] = [f for f in fs if f.action]
    assert made.action.op == "create_show_dir" and made.action.args["seasons"] == [2]
    assert [f.kind for f in fs if not f.action] == ["ab_subscription_dir_gone"]


# ------------------------------------------------------------------ 写的那一刻再核一次（人的意图只补不改）
def _existing_show_with_s2_row(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, season=2)
    return sh


def test_a_subscription_a_human_wrote_after_diagnose_is_not_overwritten(lib):
    """诊断时第 2 季还没有订阅、sidecar 也没有番组页 id；执行之前人写上了自己的订阅与 `mikan_id`：执行按此刻的文件，
    一个字都不改（AGENTS.md 第 16 条）。审查的变异 T2-17（删掉 `_op_subscribe_season` 里"这一季已经订阅"的检查）以前
    全套测试照样过。"""
    sh = _existing_show_with_s2_row(lib)
    findings = [f for f in _findings(lib) if f.action and f.action.op == "subscribe_season"]
    assert findings and findings[0].action.args["intent"] == {"mikan_id": MID}
    human = {"source": "cli", "mikan_id": "1111", "since": "2026-09-27"}
    sh.sidecar(subscriptions={"2": human}, mikan_id="1111")

    rep = lib.apply(findings)

    [rec] = rep.skipped
    assert "已经订阅" in rec["reason"]
    sc = lib.sidecar(NEW)
    assert sc.subscriptions == {"2": human} and sc.mikan_id == "1111"


def test_intent_riding_along_never_replaces_what_the_sidecar_has(lib):
    """`subscribe_season` 顺带补的意图（番组页 id、版本要求、TMDB 身份）只在 sidecar 还没有时填：已有的不同的值原样留着，
    只写下这一季的订阅。审查的变异 T2-16（`_fill_intent` 的"还是空的才填"改成一律填）以前全套测试照样过。"""
    from media_agent.kernel import Action, Finding

    lib.configure(qbit_allow_empty=True)
    sh = lib.show(NEW)
    sh.season(1).local(f"{NEW} S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}}, mikan_id="1111", require_any=["邪竜解放版"], tmdb_id=3401,
               tmdb_source="human")
    f = Finding(rule="subscribe", kind="subscribe", severity="important", summary="订阅第 2 季", show=NEW,
                action=Action(op="subscribe_season", args={
                    "show_dir": str(sh.path), "season": 2, "subscription": {"source": "cli"},
                    "intent": {"mikan_id": "2222", "require_any": ["TV 版"], "tmdb_id": 9999}}))

    rep = lib.apply([f])

    [rec] = rep.applied
    assert rec["undo"]["entries"] == [{"field": "subscriptions", "key": "2", "value": {"source": "cli"}}]
    sc = lib.sidecar(NEW)
    assert (sc.mikan_id, sc.require_any, sc.tmdb_id, sc.tmdb_source) == ("1111", ["邪竜解放版"], 3401, "human")
    assert sc.subscriptions == {"2": {"source": "cli"}}


@pytest.mark.allow("failed_record", match="create_show_dir")
def test_creating_a_dir_is_refused_when_qbittorrent_cannot_be_read(lib):
    """占用看不全（qBittorrent 的种子列表读失败）就拒绝：盘上看不到不等于没人占（AGENTS.md 第 8 条）。审查的变异 T2-13
    （`if chk.unknown:` → `if False:`）以前全套测试照样过。"""
    _row(lib)
    findings = _findings(lib)
    lib.qbit.fail("torrents", times=None)

    rep = lib.apply(findings)

    [rec] = rep.failed
    assert rec["op"] == "create_show_dir" and "无法确认" in rec["error"]
    assert not lib.path(NEW).exists()


def test_rollback_keeps_a_dir_a_torrent_now_saves_into(lib):
    """建目录之后 AB 加了一个种子（0%，盘上什么都没有）保存进 `Season 1`：目录归它了，回退不删、写明是哪个种子。审查的
    变异 T2-15（`_remove_show_dir` 的 `if owners:` → `if False:`）以前全套测试照样过——只测过盘上有东西的那一种。"""
    _row(lib, season=1)
    c = lib.cycle(detectors=[AbAdoptionDetector])
    lib.qbit.seed("f" * 40, name="[LoliHouse] Shinban Otsu - 01", save_path=lib.path(NEW) / "Season 1",
                  files={"[LoliHouse] Shinban Otsu - 01.mkv": 600_000_000}, progress=0.0,
                  category="Bangumi", tags="ab:51")

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "ffffffff" in res["skipped_detail"][0]["skip_reason"]
    assert lib.path(NEW).is_dir()


def test_intent_with_a_malformed_page_id_does_not_reach_the_sidecar(lib):
    """订阅类动作带进 sidecar 的番组页 id 只收纯数字（`actions._clean_intent`）：动作参数来自检测器或人的命令行、也可能来自
    一份旧的诊断快照，sidecar 里不该出现 `abc` 这样的页（审查的变异 T2-28 以前全套测试照样过——命令行那边先查过了）。"""
    from media_agent.actions import _clean_intent

    assert _clean_intent({"mikan_id": "4601"}) == {"mikan_id": "4601"}
    assert _clean_intent({"mikan_id": "abc"}) == {}
    assert _clean_intent({"mikan_id": 4601}) == {}
