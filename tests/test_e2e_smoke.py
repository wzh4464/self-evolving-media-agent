"""端到端冒烟：证明基座能离线跑通真实流程（扫描 → 全量内置规则 → 执行 → 回退）。

每个场景都取自真实事故，断言落在**外部可观察的结果**上——磁盘、qBittorrent
状态、审计记录——而不是内部调用。执行器与检测器都是生产代码原样。
"""
from __future__ import annotations

import json
from pathlib import Path

from harness import MikanItem, video, weekly
from media_agent.plugins.subscription import TitleMatchBrokenDetector

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def test_release_named_file_is_renamed_via_qbit_and_converges(lib):
    """有种子的文件改名必须走 renameFile（AGENTS.md 第 3 条），且几轮后到达不动点。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)

    rounds = lib.converge()

    [rec] = rounds[0].applied("rename")
    assert rec["via"] == "qbittorrent"
    assert rec["undo"] == {"op": "rename", "path": str(s1.path / "尼古喵喵 S01E08.mkv"),
                           "new_name": LOLI_08, "torrent_hash": t.hash}
    assert ("rename_file", t.hash, LOLI_08, "尼古喵喵 S01E08.mkv") in lib.qbit.calls
    assert t.view()["name"] == LOLI_08                   # 显示名不变
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176}
    assert lib.sidecar("尼古喵喵").seasons["1"]["have"] == [8]
    assert not rounds[-1].actions()                      # 不动点：没有待执行的动作


def _abema_vs_lolihouse(lib):
    """2026-09-05 尼古喵喵 S01E08：旧的 ABEMA 生肉占着集位名，LoliHouse 带双字幕的新到。"""
    s1 = lib.show("尼古喵喵").season(1)
    raw = s1.single("尼古喵喵 S01E08.mkv", size=745_065_995, name=ABEMA_08,
                    probe=video("h264"))
    sub = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)
    return s1, raw, sub


def test_duplicate_loser_goes_to_trash_and_winner_takes_the_slot(lib):
    s1, raw, sub = _abema_vs_lolihouse(lib)
    raw_ident = lib.ident(raw.path)

    c = lib.cycle()

    [dup] = [f for f in c.findings if f.kind == "duplicate"]
    assert dup.path == str(raw.path)                     # 零字幕轨的生肉输（字幕能力先于体积）
    [trash] = c.applied("trash")
    moved = Path(trash["trashed_to"])
    assert moved.is_relative_to(lib.cfg.trash_dir)
    assert lib.ident(moved) == raw_ident and moved.stat().st_size == 745_065_995
    assert trash["undo"] == {"op": "restore_from_trash", "path": str(raw.path),
                             "trash_path": str(moved), "torrent_record_lost": True}
    assert not lib.qbit.has(raw.hash)                    # 整种子作废：记录摘掉
    # 腾空排在改名之前（2026-09-04 入间同学）：同一批次里赢家就拿到了集位名
    [ren] = c.applied("rename")
    assert ren["args"]["torrent_hash"] == sub.hash
    assert lib.disk() == {"尼古喵喵/Season 1/尼古喵喵 S01E08.mkv": 593_601_176}


def test_rollback_restores_disk_exactly(lib):
    """一键回退（AGENTS.md 第 4 条）：磁盘回到原样；被删的种子记录明确报告为丢失。"""
    _abema_vs_lolihouse(lib)
    before = lib.disk()

    c = lib.cycle()
    assert {r["op"] for r in c.report.applied} >= {"trash", "rename"}
    res = lib.rollback(c.run_id)

    assert res["failed"] == 0 and res["skipped"] == 0
    assert res["reverted"] == len([r for r in c.report.applied if r.get("undo")])
    assert res["torrent_records_lost"] == 1
    assert lib.disk() == before


def test_grab_adds_pinned_torrent_and_bookkeeping_survives_the_batch(lib):
    """先到先得抓取：加种、钉 `ma:` 集号、落剧名分类、当场改名、记进 have。

    `have` 那条断言守的是 2026-09-26 的记账事故：抓取（op 0）写进 sidecar 的新集
    被同批次 write_sidecar（op 10）用诊断期快照整份盖掉。
    """
    lib.configure(qbit_allow_empty=True)        # 前 8 集是纯本地文件，qBit 里本来没有种子
    sh = lib.show("尼古喵喵")
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    item = MikanItem(title=title, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=["尼古喵喵"])

    c = lib.cycle()

    [grab] = c.applied("grab_episode")
    assert grab["undo"] == {"op": "ungrab_episode", "show_dir": str(sh.path),
                            "season": 1, "episode": 9, "title": title}
    assert grab["already_present"] is False
    v = lib.qbit.torrent(item.infohash)
    assert v["tags"] == "ma:S01E09"                       # 无订阅 id → 不给 ab:
    assert v["category"] == "尼古喵喵"                     # 自己抓的直接落剧名分类
    assert v["save_path"] == str(s1.path)
    assert v["name"] == title + ".mkv"
    assert lib.qbit.file_names(item.infohash) == ["尼古喵喵 S01E09.mkv"]
    assert 9 in lib.sidecar("尼古喵喵").seasons["1"]["have"]

    again = lib.cycle()
    assert not again.actions("grab_episode")             # 在下载中的那集不重复抓


def test_title_match_fix_goes_through_real_ab_db(lib):
    """订阅静默失效（上伊那牡丹：罗马音整体改写）。真 AutoBangumiDB + docker 替身。

    三步顺序（AGENTS.md 第 7 条）：改别名 → 清"已登记但不在 qBit"的记录 → 刷新。
    """
    rss = "https://mikanani.me/RSS/Search?searchstr=Sake+o+Tsugu"
    lib.bangumi(id=7, official_title="上伊那牡丹", title_raw="Sake o Tsugu",
                save_path=str(lib.media_root / "上伊那牡丹" / "Season 1"),
                rss_link=rss, group_name="ANi")
    lib.ab_rows("torrent", [{"bangumi_id": 7, "name": "[ANi] Sake o Tsugu - 05"},
                            {"bangumi_id": 7, "name": "[ANi] Sake o Tsugu - 04"}])
    lib.show("上伊那牡丹").season(1).single("[ANi] Sake o Tsugu - 04.mp4",
                                            name="[ANi] Sake o Tsugu - 04")
    lib.web.rss(rss, [f"[ANi] 上伊那牡丹，醉身姿如百合 / Yoeru Sugata wa Yuri no Hana - {n:02d}"
                      f" [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4" for n in (5, 6, 7)])

    c = lib.cycle(detectors=[TitleMatchBrokenDetector])

    [fix] = c.applied("fix_title_aliases")
    assert fix["cleared_stuck_records"] == 1 and fix["refreshed"] is True
    assert fix["undo"] == {"op": "restore_title_aliases", "bangumi_id": 7, "prev": "[]"}
    row = lib.abdb.query("SELECT title_aliases FROM bangumi WHERE id=7")[0]
    assert json.loads(row["title_aliases"]) == ["上伊那牡丹"]
    assert [r["name"] for r in lib.abdb.query("SELECT name FROM torrent")] == \
           ["[ANi] Sake o Tsugu - 04"]                   # 还在 qBit 里的那条不能清
    assert lib.docker_log.read_text().split() == ["stop", "autobangumi",
                                                  "start", "autobangumi"]
    assert lib.ab.calls == ["all_bangumi", "refresh_all"]
