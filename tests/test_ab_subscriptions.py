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
    """生产卷是大小写不敏感的 APFS：`re zero` 与 `Re Zero` 是同一个目录。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show("Re Zero")
    sh.season(1).local("Re Zero S01E01.mkv")
    sh.sidecar(seasons={"1": {"have": [1]}})
    _row(lib, name="re zero", season=1)

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
