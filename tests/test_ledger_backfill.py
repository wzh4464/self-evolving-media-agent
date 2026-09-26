"""补录出处账本（`media-agent ledger backfill [--dry-run]`，每轮 `run` 开头自动做一次增量）。

生产上账本开张时 539 个种子一行都没有（state 调研 H4：459 个只有 AB 的记录、6 个两边都有、43 个只有本项目
的抓取审计、31 个查不到）。补录的来源，按可信程度：

0. 本项目的抓取审计（`grab_episode` 记录）：抓取器定的集位，与 `ma:` 钉子同一个来源；
1. AutoBangumi 库的 `torrent` 表（**只读**打开）：`name` 是番组页标题，`url` 里就是 infohash；
   连带它的番组行（季、`episode_offset`）；
2. 还剩下的、所在的番 sidecar 里有 `mikan_id` 的：拉那个番组页的 feed（有缓存），按 enclosure URL 的
   文件名（= v1 infohash）认，不用下 .torrent。认不出的记一笔，`MISS_TTL` 内不再为它拉番组页。

集位按同一套命名解析算（发布方声明的季号、sidecar 的 `season_offsets`、AB 的 `episode_offset`）；换算不了的
集位留空（声明的季号照记）。只补没有的行：幂等，重复跑什么都不改。
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import pytest
from harness import MikanItem

from media_agent import cli, ledger
from media_agent import ledger_backfill as lb

REZERO = "Re：从零开始的异世界生活"
FYY_MIKAN = ("[Fyy Raws] Re:从零开始的异世界生活 第三季 / Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season"
             " - 08 [1080p][AVC AAC]")
FYY_FILE = "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][AVC AAC].mp4"
LOLI = "[LoliHouse] 尼古喵喵 / Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
H_AB = "1" * 40
H_MA = "2" * 40
H_FEED = "3" * 40
H_NONE = "4" * 40
GB = 600_000_000


def _url(h: str) -> str:
    return f"https://mikanani.me/Download/20260831/{h}.torrent"


def _rezero_ab(lib, *, offsets=None):
    """Re:Zero 第三季经 AutoBangumi 下进压平了的 Season 1（AB 订阅 9，season 1），AB 已改名成 S01E08。"""
    sh = lib.show(REZERO)
    if offsets is not None:
        sh.sidecar(season_offsets=offsets)
    sh.bangumi(9, title_raw="Re Zero kara Hajimeru Isekai Seikatsu 3rd Season", season=1)
    lib.ab_rows("torrent", [{"bangumi_id": 9, "rss_id": None, "name": FYY_MIKAN, "url": _url(H_AB),
                             "homepage": "https://mikanani.me/Home/Episode/" + H_AB, "downloaded": 1}])
    sh.season(1).single("Re:从零开始的异世界生活 S01E08.mkv", size=487_000_000, name=FYY_FILE,
                        hash=H_AB, tags="ab:9")
    return sh


def _rows(lib) -> dict:
    rows, problem = ledger.load_rows(lib.cfg.state_dir)
    assert problem == ""
    return rows


def test_an_autobangumi_torrent_is_backfilled_from_its_db(lib):
    _rezero_ab(lib)
    before = open(lib.abdb.db_path, "rb").read()

    rep = lb.backfill(lib.context())

    row = _rows(lib)[H_AB]
    assert row.source == ledger.AUTOBANGUMI and row.ab_bangumi_id == 9
    assert row.mikan_title == FYY_MIKAN and row.mikan_url == _url(H_AB)
    assert (row.declared_season, row.raw_episode) == (3, 8)
    assert row.slot is None                    # 第三季、库内压平成 Season 1、又没有换算：集位留空，不猜
    assert row.show_dir == str(lib.path(REZERO)) and not row.grabbed
    assert rep.inserted == {ledger.AUTOBANGUMI: 1} and rep.covered == rep.torrents == 1
    assert open(lib.abdb.db_path, "rb").read() == before          # AB 库只读
    assert not lib.docker_log.exists() or lib.docker_log.read_text() == ""   # 不停容器


def test_the_slot_uses_the_sidecar_offsets(lib):
    _rezero_ab(lib, offsets={"3": 50})

    lb.backfill(lib.context())

    assert _rows(lib)[H_AB].slot == (1, 58)


def test_the_episode_offset_of_the_ab_row_is_applied(lib):
    title = "超超超超超喜欢你的100个女朋友"
    sh = lib.show(title)
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    lib.ab_rows("torrent", [{"bangumi_id": 37, "name": "[ANi] 超超超超超喜欢你的100个女朋友 - 25 [1080P]",
                             "url": _url(H_AB)}])
    sh.season(3).single(f"{title} S03E01.mkv", hash=H_AB, name="[ANi] Hyakkano - 25 [1080P].mkv")

    lb.backfill(lib.context())

    assert _rows(lib)[H_AB].slot == (3, 1)


def test_an_old_grab_in_the_audit_is_backfilled_as_the_grabbers_decision(lib):
    """账本之前的抓取：审计里的 `grab_episode` 记录（URL 的文件名就是 infohash）。它的集位是抓取器定的。"""
    sh = lib.show(REZERO)
    sh.season(1).single(FYY_FILE, name=FYY_FILE, hash=H_MA, tags="ma:S01E58")
    rec = {"ts": "2026-08-31T12:28:00", "run_id": "20260831T122800", "seq": 1, "status": "applied",
           "dry_run": False, "rule": "episode-available", "kind": "episode_grabbable", "op": "grab_episode",
           "summary": "「Re」S1E58 可抓取（2 个候选中选 +0）",
           "args": {"url": _url(H_MA), "title": FYY_MIKAN, "show_dir": str(sh.path), "season": 1,
                    "episode": 58, "bangumi_id": 9, "category": REZERO, "official_title": REZERO}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    rep = lb.backfill(lib.context())

    row = _rows(lib)[H_MA]
    assert row.source == ledger.MEDIA_AGENT and row.grabbed and row.slot == (1, 58)
    assert row.grabbed_at == "2026-08-31T12:28:00" and row.run_id == "20260831T122800"
    assert row.mikan_title == FYY_MIKAN and "可抓取" in row.chosen_reason
    assert rep.inserted == {ledger.MEDIA_AGENT: 1}


def test_a_torrent_found_in_the_shows_mikan_feed(lib):
    sh = lib.show("尼古喵喵")
    sh.sidecar(mikan_id="4102")
    s1 = sh.season(1)
    s1.single("[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv", hash=H_FEED)
    s1.single("[G] Yani Neko - 09.mkv", hash="5" * 40, tags="ma:S01E09")
    lib.mikan("4102", [MikanItem(title=LOLI, pub="2026-08-22", url=_url(H_FEED)),
                       MikanItem(title=LOLI.replace("- 08", "- 09"), pub="2026-08-29", url=_url("5" * 40))])

    rep = lb.backfill(lib.context())

    rows = _rows(lib)
    assert rows[H_FEED].source == ledger.UNKNOWN            # 知道它是什么，不知道谁加的
    assert rows[H_FEED].mikan_title == LOLI and rows[H_FEED].pub_date == "2026-08-22"
    assert rows[H_FEED].slot == (1, 8) and {"简繁", "内封"} <= set(rows[H_FEED].versions)
    assert rows["5" * 40].source == ledger.MEDIA_AGENT and rows["5" * 40].slot == (1, 9)  # 钉子是抓取器的
    assert rep.inserted == {ledger.UNKNOWN: 1, ledger.MEDIA_AGENT: 1}


def test_a_torrent_nobody_knows_is_reported_and_not_looked_up_again_soon(lib):
    sh = lib.show("尼古喵喵")
    sh.sidecar(mikan_id="4102")
    sh.season(1).single("[X] Yani Neko - 10.mkv", hash=H_NONE)
    lib.mikan("4102", [MikanItem(title=LOLI, pub="2026-08-22", url=_url(H_FEED))])

    rep = lb.backfill(lib.context())
    calls = len(lib.web.calls)
    from media_agent.cache import Cache
    Cache(lib.cfg.cache_db).conn.execute("DELETE FROM llm")             # feed 缓存过期了也一样
    Cache(lib.cfg.cache_db).conn.commit()
    rep2 = lb.backfill(lib.context())

    assert [r["hash"] for r in rep.remaining] == [H_NONE] and rep.covered == 0
    assert rep.remaining[0]["show"] == "尼古喵喵"
    assert len(lib.web.calls) == calls                  # MISS_TTL 之内不再为它拉番组页
    assert [r["hash"] for r in rep2.remaining] == [H_NONE]


def test_backfill_is_idempotent(lib):
    _rezero_ab(lib)
    lb.backfill(lib.context())

    rep = lb.backfill(lib.context())

    assert rep.inserted == {} and rep.covered == 1


def test_a_dry_run_writes_nothing(lib):
    _rezero_ab(lib)

    rep = lb.backfill(lib.context(), dry_run=True)

    assert rep.inserted == {ledger.AUTOBANGUMI: 1} and rep.dry_run
    assert not ledger.path_of(lib.cfg.state_dir).exists()


def test_a_grab_row_is_never_overwritten(lib):
    _rezero_ab(lib)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_AB, mikan_title=FYY_MIKAN, season=1, episode=58, run_id="g1")

    rep = lb.backfill(lib.context())

    row = _rows(lib)[H_AB]
    assert row.source == ledger.MEDIA_AGENT and row.slot == (1, 58) and rep.inserted == {}


@pytest.mark.allow("log_failure", match="出处账本")
def test_a_broken_ledger_is_reported_not_rebuilt(lib):
    _rezero_ab(lib)
    p = ledger.path_of(lib.cfg.state_dir)
    p.write_bytes(b"junk" * 300)

    rep = lb.backfill(lib.context())

    assert rep.problems and ledger.LEDGER_NAME in rep.problems[0]
    assert p.read_bytes() == b"junk" * 300


# ------------------------------------------------------------------ 命令行与 run
@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_cli_reports_coverage(offline_cli, capsys):
    lib = offline_cli
    _rezero_ab(lib)
    lib.show("银八").season(1).single("银八 S01E01.mkv", name="[G] Gintama - 01.mkv", hash=H_NONE)

    rc = cli.cmd_ledger_backfill(_args(dry_run=True), lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0 and "1/2" in out and "autobangumi" in out and H_NONE[:8] in out
    assert not ledger.path_of(lib.cfg.state_dir).exists()


def test_every_run_backfills_incrementally_first(offline_cli, capsys):
    lib = offline_cli
    _rezero_ab(lib)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    assert _rows(lib)[H_AB].source == ledger.AUTOBANGUMI
    assert "出处账本" in capsys.readouterr().out


def test_the_ab_db_is_opened_read_only(lib, monkeypatch):
    """补录只读 AB 库：连接一律 `mode=ro`，写一句都会失败。"""
    _rezero_ab(lib)
    seen = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        seen.append((str(target), k.get("uri")))
        return real(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    lb.backfill(lib.context())

    ab = [t for t, _ in seen if lib.abdb.db_path in t]
    assert ab and all(t.startswith("file:") and "mode=ro" in t for t in ab)


def test_ledger_commands_are_wired_into_main(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    _rezero_ab(lib)
    monkeypatch.setattr("sys.argv", ["media-agent", "ledger", "backfill"])
    assert cli.main() == 0
    capsys.readouterr()

    monkeypatch.setattr("sys.argv", ["media-agent", "ledger", "show", H_AB[:8]])
    assert cli.main() == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["infohash"] == H_AB and shown["source"] == ledger.AUTOBANGUMI

    monkeypatch.setattr("sys.argv", ["media-agent", "ledger", "show", "ffffff"])
    assert cli.main() == 1


def test_a_grab_recorded_as_failed_still_counts_when_its_torrent_is_there(lib):
    """2026-09-16 … 09-26 的 12 次抓取：种子加上了、之后 NameError，审计全记 failed。种子此刻就在 qBittorrent 里，
    加种就发生过——记录里的集位照样是抓取器定的。"""
    sh = lib.show(REZERO)
    sh.season(1).single(FYY_FILE, name=FYY_FILE, hash=H_MA, tags="ma:S01E58")
    rec = {"ts": "2026-09-20T06:00:00", "run_id": "20260920T060000", "status": "failed",
           "dry_run": False, "rule": "episode-available", "op": "grab_episode", "summary": "S1E58 可抓取",
           "error": "NameError: name 'resp' is not defined",
           "args": {"url": _url(H_MA), "title": FYY_MIKAN, "show_dir": str(sh.path), "season": 1,
                    "episode": 58}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    lb.backfill(lib.context())

    row = _rows(lib)[H_MA]
    assert row.source == ledger.MEDIA_AGENT and row.slot == (1, 58) and row.mikan_title == FYY_MIKAN


def test_tags_are_the_last_resort(lib):
    """审计、AB 库、番组页里都没有：`ma:` 钉子只由本项目的抓取打（集位就是钉子）；`manual:` 是人手加种时打的。"""
    s1 = lib.show("幼女战记").season(2)
    s1.single("[Nix-Raws] Youjo Senki S02E12 [CR WEB-DL 1080p].mkv", hash=H_MA, tags="ab:28, ma:S02E12")
    lib.show("抓娃娃 (2024)").folder("").single("抓娃娃 (2024).mkv", hash=H_NONE, tags="manual:movie")
    lib.show("银八").season(1).single("[G] Gintama - 01.mkv", hash=H_FEED)

    rep = lb.backfill(lib.context())

    rows = _rows(lib)
    assert rows[H_MA].source == ledger.MEDIA_AGENT and rows[H_MA].slot == (2, 12)
    assert rows[H_MA].mikan_title == ""                       # 标题不知道，不编
    assert rows[H_NONE].source == ledger.MANUAL and rows[H_NONE].slot is None
    assert [r["hash"] for r in rep.remaining] == [H_FEED]


def _audit_409(lib):
    rec = {"ts": "2026-08-31T13:00:00", "run_id": "20260831T130000", "status": "applied",
           "dry_run": False, "rule": "episode-available", "op": "grab_episode", "summary": "S1E58 可抓取",
           "already_present": True,
           "args": {"url": _url(H_AB), "title": FYY_MIKAN, "show_dir": str(lib.path(REZERO)),
                    "season": 1, "episode": 58}}
    lib.cfg.audit_log.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
def test_a_409_grab_in_the_audit_does_not_claim_the_torrent(lib, dry_run):
    """审计里这个 infohash 只有 409 的抓取（种子早就在）：集位照样是抓取器的，但不冒认是本项目加的——
    来源记 unknown，AB 库认得出就补成 autobangumi。报告里它只算一行、算在 autobangumi 名下：以前先按 unknown 数一次、
    升级时又按 autobangumi 数一次，生产重放报「unknown 10」而账本里只有 8 行、各来源合计 526 对不上覆盖率 524
    （2026-09-27 审查）。"""
    _rezero_ab(lib)
    _audit_409(lib)

    rep = lb.backfill(lib.context(), dry_run=dry_run)

    assert rep.inserted == {ledger.AUTOBANGUMI: 1} and rep.upgraded == {}
    assert sum(rep.inserted.values()) == rep.covered - rep.rows_before == 1
    if not dry_run:
        row = _rows(lib)[H_AB]
        assert row.slot == (1, 58) and row.grabbed                   # 抓取器定的集位
        assert row.source == ledger.AUTOBANGUMI and row.ab_bangumi_id == 9


def test_an_existing_row_of_unknown_origin_that_gets_its_source_is_an_upgrade_not_an_insert(lib):
    """账本里早就有一行（409 时抓取器记的 unknown），这次 AB 库认出是谁加的：报成"认出来源"，不算补录了一行。"""
    _rezero_ab(lib)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_AB, mikan_title=FYY_MIKAN, season=1, episode=58, added=False)

    rep = lb.backfill(lib.context())

    assert rep.inserted == {} and rep.upgraded == {ledger.AUTOBANGUMI: 1}
    assert "认出来源 autobangumi 1" in rep.summary()
    assert _rows(lib)[H_AB].source == ledger.AUTOBANGUMI


def test_a_known_row_of_unknown_origin_does_not_refetch_the_feed(lib):
    """已有一行、只是不知道谁加的（409 时记的）：番组页答不了"谁加的"，每轮开头的补录不为它拉番组页。"""
    sh = lib.show("尼古喵喵")
    sh.sidecar(mikan_id="4102")
    sh.season(1).single("[X] Yani Neko - 10.mkv", hash=H_NONE)
    lib.mikan("4102", [MikanItem(title=LOLI, pub="2026-08-22", url=_url(H_FEED))])
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_NONE, mikan_title=LOLI.replace("- 08", "- 10"), season=1, episode=10,
                        added=False)

    rep = lb.backfill(lib.context())

    assert lib.web.calls == [] and rep.remaining == [] and rep.inserted == {}
