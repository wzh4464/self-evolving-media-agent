"""抓取当场记出处账本（`_op_grab_episode` → `ledger.record_grab`），回退抓取把那一行标成撤销。

为什么（state 调研 H1）：抓取那一刻手上有番组页标题、抓取器按番组页 + 播出日期定的集位、偏好评分、
发布日期、落选了几个——以前只进了 Finding 的 evidence，审计只存 `args` 与摘要，发布日期与结构化的评分
哪儿都没留下；之后每条规则都从文件名重新猜这个种子是哪一集、是什么版本。

- 加种成功（或 409 已经在、或出错后核实在）之后当场记一行，infohash 从 .torrent 算；
- 预演不记；
- 账本写不进去（坏了）不让抓取变成失败：种子已经加进去了，审计记一笔 `ledger_error`；
- 回退抓取（`ungrab_episode`）不删行，标成撤销（它仍然是那个种子，只是不再替它的集位作保）。
"""
from __future__ import annotations

import pytest

from media_agent import ledger
from media_agent.kernel import Action, Finding

SHOW = "Re：从零开始的异世界生活"
FYY = "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][AVC AAC]"


def grab_finding(show_dir, url, *, ep=58, title=FYY, bangumi_id=9) -> Finding:
    return Finding(
        rule="episode-available", kind="episode_grabbable", severity="important",
        summary=f"S01E{ep:02d} 可抓取（3 个候选中选 +0）", show=show_dir.name, subject=f"S01E{ep:02d}",
        evidence={"season": "1", "episode": ep, "air_date": "2026-08-30", "mikan_id": "4101",
                  "chosen": title, "chosen_pub": "2026-08-31", "rejected_by_date": 1,
                  "rejected_by_season": ["标的是第 4 季 | x"], "verdict": "+0",
                  "verdict_detail": {"acceptable": True, "score": 0, "passed": [], "penalties": [],
                                     "blocked_by": ""},
                  "candidates": 3, "rejected": ["-30 -仅繁中 | y", "-90 -删减版 | z"]},
        action=Action(op="grab_episode", args={
            "url": url, "title": title, "show_dir": str(show_dir), "season": 1, "episode": ep,
            "bangumi_id": bangumi_id, "category": SHOW, "official_title": SHOW}))


@pytest.fixture
def show(lib):
    sh = lib.show(SHOW)
    sh.season(1)
    return sh


def _row(lib, h):
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        return led.get(h)


def test_a_grab_records_what_the_torrent_is(lib, show):
    url, h = lib.web.torrent(FYY)

    rep = lib.apply([grab_finding(show.path, url)], run_id="g1")

    [rec] = rep.applied
    row = _row(lib, h)
    assert row is not None and row.source == ledger.MEDIA_AGENT and row.active
    assert row.slot == (1, 58)                                    # 抓取器定的集位，不是标题里的 08
    assert (row.declared_season, row.raw_episode) == (3, 8)
    assert row.mikan_title == FYY and row.mikan_url == url
    assert row.pub_date == "2026-08-31" and row.show_dir == str(show.path)
    assert row.ab_bangumi_id == 9 and row.run_id == "g1" and row.grabbed_at
    assert row.verdict["acceptable"] is True and row.verdict["score"] == 0
    assert "3 个候选" in row.chosen_reason
    assert row.evidence["rejected"] == 2 and row.evidence["rejected_by_date"] == 1
    assert row.evidence["mikan_id"] == "4101" and row.evidence["air_date"] == "2026-08-30"
    assert rec["infohash"] == h and rec["ledger"] == "recorded"
    assert rec["undo"]["infohash"] == h


def test_a_409_is_recorded_too(lib, show):
    """qBittorrent 早就有这个种子（409）：抓取器对集位的判断照样记下；谁加的说不清，来源记 unknown。"""
    url, h = lib.web.torrent(FYY)
    blob = lib.web.urlopen(url).read()
    lib.qbit.add_torrent(blob, save_path=str(show.path / "Season 1"), category=SHOW)

    rep = lib.apply([grab_finding(show.path, url)])

    [rec] = rep.applied
    assert rec["already_present"] is True
    row = _row(lib, h)
    assert row.slot == (1, 58) and row.source == ledger.UNKNOWN
    assert any("409" in n for n in row.notes)


def test_a_dry_run_grab_records_nothing(lib, show):
    url, h = lib.web.torrent(FYY)

    lib.apply([grab_finding(show.path, url)], dry_run=True)

    assert ledger.load_rows(lib.cfg.state_dir) == ({}, "")


@pytest.mark.allow("log_failure", match="出处账本")
def test_a_broken_ledger_does_not_fail_the_grab(lib, show):
    (lib.cfg.state_dir / ledger.LEDGER_NAME).write_bytes(b"garbage" * 200)
    url, h = lib.web.torrent(FYY)

    rep = lib.apply([grab_finding(show.path, url)])

    [rec] = rep.applied                                  # 种子加进去了：抓取照样是 applied、带逆操作
    assert lib.qbit.has(h) and rec["undo"]["op"] == "ungrab_episode"
    assert "ledger_error" in rec and ledger.LEDGER_NAME in rec["ledger_error"]
    assert (lib.cfg.state_dir / ledger.LEDGER_NAME).read_bytes() == b"garbage" * 200


def test_rolling_back_a_grab_retracts_the_row(lib, show):
    url, h = lib.web.torrent(FYY)
    lib.apply([grab_finding(show.path, url)], run_id="g1")

    res = lib.rollback("g1")

    assert (res["reverted"], res["failed"]) == (1, 0)
    row = _row(lib, h)
    assert row is not None and row.status == ledger.RETRACTED     # 行留着：它仍然是那个种子
    assert any("g1" in n for n in row.notes)
    assert lib.qbit.has(h)                                        # 回退抓取不动种子


@pytest.mark.parametrize("backfilled_slot", [None, (1, 8)], ids=["no-slot", "ab-slot"])
def test_a_backfilled_row_is_superseded_by_the_grab(lib, show, backfilled_slot):
    """补录（AB 登记的）在前，抓取器后来也选中了它（409）：集位按抓取器的，加的人仍是 AB。补录那一行已经有集位——
    Re:Zero 的形态：AB 改名后按标题补录成 S01E08——抓取器的 S01E58 照样盖过它（2026-09-27 审查：以前补录都不带集位，
    变异"保留之前的集位"全套存活）。"""
    url, h = lib.web.torrent(FYY)
    blob = lib.web.urlopen(url).read()
    lib.qbit.add_torrent(blob, save_path=str(show.path / "Season 1"), category=SHOW)
    s, e = backfilled_slot or (None, None)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.upsert_backfill(infohash=h, source=ledger.AUTOBANGUMI, mikan_title=FYY,
                            show_dir=str(show.path), season=s, episode=e)
        assert led.get(h).slot == backfilled_slot

    lib.apply([grab_finding(show.path, url)])

    row = _row(lib, h)
    assert row.source == ledger.AUTOBANGUMI and row.slot == (1, 58) and row.grabbed_at


def test_the_grab_detector_hands_the_ledger_a_structured_verdict(lib):
    """detector → executor → 账本这条契约：账本要的结构化评分从抓取发现的 `evidence.verdict_detail` 来。以前账本测试
    都手写这个 evidence，抓取检测器改了键名、账本里就只剩 `{}`——变异全套存活（2026-09-27 审查）。"""
    from harness import MikanItem, weekly
    from media_agent.plugins.grab import EpisodeAvailableDetector
    lib.configure(qbit_allow_empty=True)
    name = "尼古喵喵"
    schedule = weekly(12, first_days_ago=60)
    sh = lib.show(name)
    for n in range(1, 9):
        sh.season(1).local(f"{name} S01E{n:02d}.mkv")
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=name, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title="[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
                     pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=[name])

    c = lib.cycle(detectors=[EpisodeAvailableDetector])

    assert c.applied("grab_episode")
    row = _row(lib, item.infohash)
    assert row is not None and row.grabbed and row.slot == (1, 9)
    assert {"acceptable", "score", "passed", "penalties", "blocked_by"} <= set(row.verdict)
    assert row.verdict["acceptable"] is True and row.evidence.get("candidates") == 1


def test_grabbing_a_retracted_row_again_makes_it_vouch_again(lib, show):
    """回退过的抓取（行标成撤销）又被抓了一次：行恢复作保、留一笔（2026-09-27 审查：变异"保持撤销"全套存活）。"""
    url, h = lib.web.torrent(FYY)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=h, mikan_title=FYY, season=1, episode=58, run_id="g1")
        led.retract(h, run_id="rb1", why="回退抓取 g1")
        assert not led.get(h).active

        row = led.record_grab(infohash=h, mikan_title=FYY, season=1, episode=58, run_id="g2")

    assert row.active and row.status == ledger.ACTIVE
    assert any("恢复作保" in n for n in row.notes)


def test_a_v2_infohash_in_a_torrent_url_is_not_read_as_a_v1_one():
    """`<64 位十六进制>.torrent`（BitTorrent v2 的 infohash）不是 v1 的：不能截出后 40 位当 infohash（变异存活）。"""
    v2 = "ab" * 32
    assert ledger.infohash_of_url(f"https://mikanani.me/Download/20260901/{v2}.torrent") is None
    v1 = "c" * 40
    assert ledger.infohash_of_url(f"https://mikanani.me/Download/20260901/{v1}.torrent") == v1
