"""隔离区处置按出处账本认替代者与集位（`purge._identity_problem` / `_slot_of` / `_rank_problem`）。

证明"库里那份替代得了隔离区里这份"要先证明它**是这一集**。以前的证据：钉子，或种子显示名（`_release_slot`）按
同一套换算落在这个集位上。显示名是 .torrent 的内部名，常常不写季号：`[Fyy Raws] Re Zero - 08` 这样的内部名
"认得出" S01E08，而番组页标题明写的是「第三季」——2026-08-31 那份就是按错口径改名的第 58 集。反过来，
内部名认不出集号（`[G] Re Zero [1080p]`）的替代者，哪怕是抓取器亲手定的集位也不作保，原片永远等人。

现在账本先说话：
- 账本记下的集位（钉子、抓取器定的、声明了季号按此刻换算得出的）等于这个集位 → 作保；不等 → 不作保；
- 账本说"声明了别的季、换算不了" → 不作保（名字里的集号可能是按那一季编的）；
- 隔离记录里认不出集位时（关口没记下、摘要里没有、原文件名不写季号），按被隔离那个种子在账本里的集位认；
- 复排（I4）时隔离的那份也带上它的番组页标题。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from harness import video

from media_agent import disposal, ledger

REZERO = "Re：从零开始的异世界生活"
FYY_MIKAN = ("[Fyy Raws] Re:从零开始的异世界生活 第三季 / Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season"
             " - 08 [1080p][AVC AAC]")
H_SURV, H_GONE = "a1" * 20, "b2" * 20
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


@pytest.fixture
def no_rmtree(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("隔离区里不许 rmtree"))


def _quarantined(lib, name, *, slot=(1, 8), subject=None, summary="S01E08 重复：…", days_ago=40,
                 size=1_410_655_350):
    """隔离区里的原片 + 一条带 `deletion` 的 trash 记录（保留期已过）。"""
    import json
    ts = datetime.now() - timedelta(days=days_ago)
    origin = lib.media_root / REZERO / "Season 1" / name
    p = lib.cfg.trash_dir / f"{ts:%Y-%m-%d}" / REZERO / "Season 1" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as fh:
        fh.truncate(size)
    rec = {"ts": ts.isoformat(timespec="seconds"), "run_id": f"{ts:%Y%m%dT%H%M%S}", "status": "applied",
           "dry_run": False, "rule": "duplicate-episode", "kind": "duplicate", "op": "trash",
           "args": {"path": str(origin)}, "summary": summary, "trashed_to": str(p), "freed_bytes": size,
           "deletion": {"gate": "passed", "disposition": "duplicate",
                        "slot": list(slot) if slot else None, "subject": subject or {}},
           "undo": {"op": "restore_from_trash", "path": str(origin), "trash_path": str(p)}}
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def test_a_survivor_whose_mikan_title_is_another_season_does_not_vouch(lib, no_rmtree):
    """内部名 `[Fyy Raws] Re Zero - 08` 按显示名认得出 S01E08（以前就此作保、原片到期硬删）；
    账本里的番组页标题明写第三季、sidecar 没有换算——不作保。"""
    moved = _quarantined(lib, f"{REZERO} S01E08.mkv")
    lib.show(REZERO).season(1).single("Re:从零开始的异世界生活 S01E08.mkv", size=487_000_000,
                                      name="[Fyy Raws] Re Zero - 08 [1080p].mp4", hash=H_SURV)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.upsert_backfill(infohash=H_SURV, source=ledger.AUTOBANGUMI, mikan_title=FYY_MIKAN)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert not c.eligible and "第 3 季" in c.why


def test_without_the_ledger_the_display_name_still_vouches(lib, no_rmtree):
    """对照：账本里没有它——显示名认得出 S01E08，照旧作保（第 2 阶段的行为）。"""
    moved = _quarantined(lib, f"{REZERO} S01E08.mkv")
    lib.show(REZERO).season(1).single("Re:从零开始的异世界生活 S01E08.mkv", size=487_000_000,
                                      name="[Fyy Raws] Re Zero - 08 [1080p].mp4", hash=H_SURV)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [moved]


def test_a_survivor_the_grabber_placed_vouches_even_when_its_name_cannot(lib, no_rmtree):
    """内部名认不出集号（`[G] Re Zero [1080p]`），以前"不作保、交给人"；账本里抓取器定的集位就是 S01E08——
    与钉子同一个来源，作保。"""
    moved = _quarantined(lib, f"{REZERO} S01E08.mkv")
    lib.show(REZERO).season(1).single(f"{REZERO} S01E08.mkv", size=1_410_655_350,
                                      name="[G] Re Zero [1080p].mkv", hash=H_SURV)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_SURV, mikan_title="[G] Re Zero - 08 [1080p]", season=1, episode=8,
                        run_id="g0")

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [moved]


def test_the_slot_of_a_record_comes_from_the_ledger_when_nothing_else_names_it(lib, no_rmtree):
    """关口没记下集位、摘要里没有、原文件名不写季号（`- 08`）：按被隔离的那个种子在账本里的集位认。"""
    moved = _quarantined(lib, "[Fyy Raws] Re Zero - 08 [1080p].mp4", slot=None, summary="重复",
                         subject={"torrent_hash": H_GONE, "name": "[Fyy Raws] Re Zero - 08 [1080p].mp4",
                                  "torrent_files": 1}, size=487_000_000)
    lib.show(REZERO).season(1).single(f"{REZERO} S01E58.mkv", size=1_300_000_000, tags="ma:S01E58",
                                      name="[G] Re Zero - 58 [1080p].mkv", probe=CHI)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_GONE, mikan_title=FYY_MIKAN, season=1, episode=58, run_id="g0")

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    [c] = rep.pool
    assert c.slot == (1, 58)
    assert [x.trash_path for x in rep.deleted] == [moved]
