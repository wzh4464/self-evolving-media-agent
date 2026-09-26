"""出处账本（`media_agent/ledger.py`，`state/ledger.sqlite`）的存储契约。

为什么要它（2026-09-27 第 4 阶段）：一个种子**是什么**——番组页上的发布标题（带「邪竜解放版」「简繁内封字幕」
这些内部名里没有的字）、抓取器按番组页 + 播出日期定下的集位、发布方声明的季号——只在两个时刻知道得最清楚：
抓取那一刻（`_op_grab_episode`）与 AutoBangumi 登记它那一刻（AB 库的 `torrent` 表）。此后每条规则都在从文件名
重新猜：AB 把 `3rd Season - 08` 改成 `S01E08`，判重就把 2016 年真正的第 8 集清进了隔离区（2026-08-31）；
抓取的评分、发布日期、落选了几个只进了 Finding 的 evidence，审计里没有（state.md H1）。账本按 infohash 记下来，
之后谁要问"这个种子是哪一集、是哪个版本"就查它。

契约：
- 一个 infohash 一行；抓取写的（`record_grab`）是定论，补录（`upsert_backfill`）只填空、不覆盖；
- 回退抓取只把那一行标成撤销（`retract`），不删——它仍然是那个种子，只是我们不再替它的集位作保；
- 打不开、坏了、结构版本比代码新：`LedgerUnavailable`，文件原样不动；`load_rows` 永不抛，给调用方一句原因，
  调用方按没有账本的老办法走（健康报告说出来），绝不拦下一轮。
"""
from __future__ import annotations

import sqlite3

import pytest

from media_agent import ledger

H1 = "a" * 40
H2 = "b" * 40
FYY = "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][AVC AAC]"
LOLI = "[LoliHouse] 尼古喵喵 / Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


@pytest.fixture
def led(project_root):
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    lg = ledger.Ledger.open(state)
    yield lg
    lg.close()


def _grab(led, h=H1, **kw):
    base = dict(infohash=h, mikan_title=FYY, mikan_url=f"https://mikanani.me/Download/20260831/{h}.torrent",
                pub_date="2026-08-31", show_dir="/Media/Re：从零开始的异世界生活", season=1, episode=58,
                verdict={"acceptable": True, "score": 0, "passed": [], "penalties": []},
                chosen_reason="3 个候选中选 +0", ab_bangumi_id=9, run_id="t001", added=True,
                evidence={"rejected": 2})
    base.update(kw)
    return led.record_grab(**base)


def test_a_new_ledger_is_wal_with_a_busy_timeout_and_a_schema_version(led, project_root):
    path = project_root / "state" / ledger.LEDGER_NAME
    assert path.is_file()
    assert led.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert led.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert led.conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION


def test_a_grab_row_answers_what_the_torrent_is(led):
    row = _grab(led, infohash=H1.upper())                    # qBittorrent 给小写；URL 里可能大写

    got = led.get(H1)
    assert got == row and got.source == "media-agent" and got.active
    assert (got.season, got.episode) == (1, 58) and got.slot == (1, 58)
    assert (got.declared_season, got.raw_episode) == (3, 8)       # 发布方的编号一并记下
    assert got.pub_date == "2026-08-31" and got.ab_bangumi_id == 9
    assert got.evidence == {"rejected": 2} and got.grabbed_at
    assert led.slot_of(H1) == (1, 58) and led.title_of(H1) == FYY
    assert led.slot_of(H2) is None and led.title_of(H2) == "" and led.get(H2) is None


def test_versions_come_from_the_preference_keywords_in_the_mikan_title(led):
    row = _grab(led, mikan_title=LOLI, season=1, episode=8)
    assert {"简繁", "内封"} <= set(row.versions)                  # 内部名 ASSx2 里一个都没有
    assert ledger.versions_of("[G] Yani Neko - 11 [邪竜解放版][BDRip 1080p]") >= {"邪竜解放版", "BDRip"}


def test_backfill_fills_gaps_and_never_overwrites_a_grab(led):
    _grab(led)

    assert led.upsert_backfill(infohash=H1, source="autobangumi", mikan_title="别的标题",
                               show_dir="/Media/x", season=1, episode=8) is False
    assert led.get(H1).source == "media-agent" and led.get(H1).slot == (1, 58)

    assert led.upsert_backfill(infohash=H2, source="autobangumi", mikan_title=LOLI,
                               show_dir="/Media/尼古喵喵", season=1, episode=8, ab_bangumi_id=7,
                               chosen_reason="AutoBangumi 登记") is True
    assert led.upsert_backfill(infohash=H2, source="autobangumi", mikan_title=LOLI,
                               show_dir="/Media/尼古喵喵", season=1, episode=8) is False   # 幂等
    assert led.get(H2).source == "autobangumi" and led.get(H2).raw_episode == 8
    assert sorted(led.rows()) == [H1, H2]


def test_a_grab_of_a_torrent_autobangumi_added_keeps_who_added_it(led):
    """409：种子早由 AB 加过。抓取器对集位的判断是定论（更新），加的人仍是 AB（来源不改），并记一笔。"""
    led.upsert_backfill(infohash=H1, source="autobangumi", mikan_title=FYY, show_dir="/Media/r",
                        season=None, episode=None)

    row = _grab(led, added=False)

    assert row.source == "autobangumi" and row.slot == (1, 58)
    assert any("409" in n or "已经在" in n for n in row.notes)


def test_retract_marks_the_row_and_keeps_it(led):
    _grab(led)

    assert led.retract(H1, run_id="rb-t001", why="回退抓取 t001") is True

    row = led.get(H1)
    assert row is not None and row.status == ledger.RETRACTED and not row.active
    assert led.slot_of(H1) is None                   # 不再替它的集位作保
    assert any("回退抓取" in n for n in row.notes)
    assert led.retract(H2, run_id="rb", why="x") is False


def test_a_corrupt_ledger_is_reported_and_left_alone(project_root):
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / ledger.LEDGER_NAME
    junk = b"this is not a sqlite database" * 100
    path.write_bytes(junk)

    with pytest.raises(ledger.LedgerUnavailable):
        ledger.Ledger.open(state)
    rows, problem = ledger.load_rows(state)

    assert rows == {} and problem and ledger.LEDGER_NAME in problem
    assert path.read_bytes() == junk                 # 不覆盖、不重建：账本坏了要人看


def test_a_ledger_from_a_newer_version_is_not_touched(project_root):
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / ledger.LEDGER_NAME
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={ledger.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(ledger.LedgerUnavailable, match="结构版本"):
        ledger.Ledger.open(state)
    _, problem = ledger.load_rows(state)
    assert "结构版本" in problem


def test_load_rows_without_a_ledger_is_empty_and_quiet(project_root):
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    assert ledger.load_rows(state) == ({}, "")
    assert not (state / ledger.LEDGER_NAME).exists()          # 只读的一方不凭空建库


def test_two_connections_share_one_ledger(project_root):
    """`run` 与人手跑的 `ledger backfill` 可能同时开着：WAL 下一个在写，另一个照样读得到已提交的。"""
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    a, b = ledger.Ledger.open(state), ledger.Ledger.open(state)
    try:
        _grab(a)
        assert b.slot_of(H1) == (1, 58)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("url,want", [
    (f"https://mikanani.me/Download/20260831/{H1}.torrent", H1),
    (f"https://mikanani.me/Download/20260831/{H1.upper()}.torrent", H1),
    (f"magnet:?xt=urn:btih:{H2}&dn=x", H2),
    ("https://mikanani.me/Download/fake/0001.torrent", None),
])
def test_the_infohash_is_read_off_the_url(url, want):
    assert ledger.infohash_of_url(url) == want


def test_bad_infohashes_are_refused(led):
    with pytest.raises(ValueError):
        led.upsert_backfill(infohash="not-a-hash", source="unknown")
    with pytest.raises(ValueError):
        led.upsert_backfill(infohash=H1, source="somebody")
