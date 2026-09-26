"""`run` 的隔离区处置：按处置类别决定什么能到期真删，不再按日期整目录 `rmtree`。

整改前（`Executor.purge_trash`）：超过 `TRASH_RETENTION_DAYS` 的 `YYYY-MM-DD` 目录整个 `rmtree`，
不看是什么、不写记录。生产上它已经删掉 6 个文件（4.7 GB，run.log:21835 / 22103）；隔离区里
`purge` 明确拒绝的 8 个文件（手工 version_swap 的 NUKITASHI 字幕与尼古喵喵 TV 版、合并发布的
另一版本）会在 2026-10-14 之后的第一轮被照删。

处置类别（记录里的 `deletion.disposition`，旧记录按规则推断）：
- `extras`（特典 / 菜单 / PV / OP-ED）：用户口径就是要删，过了保留期逐个删、逐个记；
- `dead_partial`（死种的 `.!qB` 半成品）：过了保留期删；
- `duplicate`：`purge.build_pool` 证明安全、且过了保留期才删；
- `bundled_version` / `manual` / `other` / 没有隔离记录的：**永不自动删**，过了保留期在输出里报给人。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from harness import video
from media_agent import cli, disposal, purge

GB = 600_000_000
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")


# ------------------------------------------------------------------ 现场
def _wal(lib) -> list[dict]:
    p = lib.cfg.state_dir / disposal.LOG_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _deleted(lib) -> set[str]:
    done = {r["id"] for r in _wal(lib) if r.get("op") == "purge" and r["phase"] == "done"}
    return {r["path"] for r in _wal(lib)
            if r.get("op") == "purge" and r["phase"] == "intent" and r["id"] in done}


def legacy(lib, name, *, rule, kind, days_ago, show="尼古喵喵", season="Season 1",
           summary="合成记录", deletion=None, args=None, size=1000, audit=True):
    """隔离区里的一个文件 + 一条（第 2 阶段之前格式的）trash 审计记录。"""
    ts = datetime.now() - timedelta(days=days_ago)
    origin = lib.media_root / show / season / name
    p = lib.cfg.trash_dir / f"{ts:%Y-%m-%d}" / show / season / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    if audit:
        rec = {"ts": ts.isoformat(timespec="seconds"), "run_id": f"{ts:%Y%m%dT%H%M%S}",
               "status": "applied", "dry_run": False, "rule": rule, "kind": kind, "op": "trash",
               "args": {"path": str(origin), **(args or {})}, "summary": summary,
               "trashed_to": str(p), "freed_bytes": size,
               "undo": {"op": "restore_from_trash", "path": str(origin), "trash_path": str(p),
                        "torrent_record_lost": False}}
        if deletion is not None:
            rec["deletion"] = deletion
        with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def _nikoniko(lib):
    """尼古喵喵 S01E05：带简繁双字幕的 LoliHouse 版会赢，生肉进隔离区（整种子作废）。"""
    s1 = lib.show("尼古喵喵").season(1)
    good = s1.single("[LoliHouse] Yani Neko - 05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
                     size=GB - 7, probe=CHI)
    raw = s1.single("[Raw] Yani Neko - 05 (1080p AVC).mkv", size=GB + 100_000_000, probe=RAW)
    return s1, good, raw


def _trash_the_raw_one(lib):
    s1, good, raw = _nikoniko(lib)
    ident = lib.ident(raw.path)
    c = lib.cycle()
    [rec] = c.applied("trash")
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident
    return s1, good, moved


def _later(days):
    return datetime.now() + timedelta(days=days)


@pytest.fixture
def no_rmtree(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("隔离区里不许 rmtree"))


# ------------------------------------------------------------------ run 不再整目录 rmtree
@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.show("朱音落语").season(1).single("朱音落语 S01E01.mp4", name="[G] Akane - 01.mp4")
    return lib


def _run_args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=True, max_proposals=0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_deletes_expired_extras_one_by_one_and_keeps_what_needs_a_human(
        offline_cli, capsys, no_rmtree):
    lib = offline_cli
    extra = legacy(lib, "尼古喵喵 [Tokuten][01].mkv", rule="extras-in-library", kind="extra",
                   days_ago=40)
    bundle = legacy(lib, "【7月】尼古喵喵 11【TV版】.mp4", rule="duplicate-episode",
                    kind="bundled_version", days_ago=40,
                    summary="S01E11 同一个种子里还装着 …")
    manual = legacy(lib, "尼古喵喵 10【TV版】.mp4", rule="manual", kind="manual", days_ago=40)
    swap = legacy(lib, "NUKITASHI S01E01.chs.sup", rule="-", kind="-", days_ago=40,
                  show="NUKITASHI", audit=False)

    rc = cli.cmd_run(_run_args(), lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0
    assert not extra.exists()
    assert _deleted(lib) == {str(extra)}                 # 逐个文件、先意图后删
    assert bundle.exists() and manual.exists() and swap.exists()
    assert "Tokuten" in out                              # 删了什么，run.log 里看得见
    for p in (bundle, manual, swap):                     # 过了保留期、需要人看的，逐个报出来
        assert p.name in out
    assert "需人工处置" in out


def test_run_leaves_everything_younger_than_retention(offline_cli, no_rmtree):
    lib = offline_cli
    extra = legacy(lib, "尼古喵喵 [Tokuten][01].mkv", rule="extras-in-library", kind="extra",
                   days_ago=29)

    assert cli.cmd_run(_run_args(), lib.cfg) == 0

    assert extra.exists() and _wal(lib) == []


def test_run_dry_run_deletes_nothing_and_writes_nothing(offline_cli, capsys, no_rmtree):
    lib = offline_cli
    extra = legacy(lib, "尼古喵喵 [Tokuten][01].mkv", rule="extras-in-library", kind="extra",
                   days_ago=40)

    assert cli.cmd_run(_run_args(dry_run=True), lib.cfg) == 0

    assert extra.exists() and _wal(lib) == []
    assert "Tokuten" in capsys.readouterr().out


# ------------------------------------------------------------------ 处置类别
LEGACY = [
    ("extras-in-library", "extra", "extras"),
    ("duplicate-episode", "duplicate", "duplicate"),
    ("duplicate-episode", "bundled_version", "bundled_version"),
    ("dead-torrent", "dead_torrent", "dead_partial"),
    ("manual", "manual", "manual"),
    ("title-drift", "title_drift", "other"),
]


@pytest.mark.parametrize("rule,kind,want", LEGACY, ids=[w for *_, w in LEGACY])
def test_legacy_records_infer_the_disposition_from_the_rule(lib, rule, kind, want):
    p = legacy(lib, "x.mkv", rule=rule, kind=kind, days_ago=1)

    [c] = purge.build_pool(lib.context())

    assert c.trash_path == p and c.disposition == want


def test_files_without_any_record_are_other(lib):
    legacy(lib, "x.mkv", rule="-", kind="-", days_ago=40, audit=False)

    [c] = purge.build_pool(lib.context())

    assert c.disposition == "other" and not c.eligible
    assert c.expired                                     # 日目录名给出隔离日期


def test_recorded_disposition_wins_over_the_rule(lib):
    """新记录的 `deletion.disposition` 由删除关口按规则定好，直接用。"""
    legacy(lib, "x.mkv", rule="duplicate-episode", kind="duplicate", days_ago=1,
           deletion={"gate": "passed", "disposition": "bundled_version"})

    [c] = purge.build_pool(lib.context())

    assert c.disposition == "bundled_version"


def test_dead_partial_expires_only_if_it_really_is_a_partial(lib):
    """第 1 阶段之前的死种处置搬的是 content_path（可能是完整的正片，甚至整季）；
    只有 `.!qB` 半成品才按"半成品"到期删，其余交给人。"""
    part = legacy(lib, "尼古喵喵 S01E11.mkv.!qB", rule="dead-torrent", kind="dead_torrent",
                  days_ago=40)
    whole = legacy(lib, "尼古喵喵 S01E12.mkv", rule="dead-torrent", kind="dead_torrent",
                   days_ago=40)

    pool = {c.trash_path: c for c in purge.build_pool(lib.context())}

    assert pool[part].eligible
    assert not pool[whole].eligible and "半成品" in pool[whole].why


def test_new_extras_record_from_a_real_cycle_expires(lib, no_rmtree):
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("尼古喵喵 S01E01.mkv", name="[G] Yani Neko - 01.mkv")
    s1.single("尼古喵喵 NCOP.mkv", size=90_000_000, name="[G] Yani Neko NCOP.mkv")
    c = lib.cycle()
    [rec] = c.applied("trash")
    assert rec["deletion"]["disposition"] == "extras"
    [moved] = lib.trash_files()

    early = disposal.dispose(lib.context(), mode="run", run_id="p001", now=_later(29))
    assert moved.exists() and not early.deleted

    rep = disposal.dispose(lib.context(), mode="run", run_id="p002", now=_later(31))

    assert [c.trash_path for c in rep.deleted] == [moved]
    assert not moved.exists() and _deleted(lib) == {str(moved)}


# ------------------------------------------------------------------ 判重：证明安全 + 过保留期
def test_run_deletes_a_verified_duplicate_only_after_retention(lib, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001", now=_later(10))
    [c] = rep.pool
    assert c.disposition == "duplicate" and c.eligible      # 证明得了……
    assert moved.exists() and not rep.deleted               # ……但还在保留期里：留着给回退

    rep = disposal.dispose(lib.context(), mode="run", run_id="p002", now=_later(31))

    assert not moved.exists()
    [intent] = [r for r in _wal(lib) if r.get("phase") == "intent"]
    assert intent["disposition"] == "duplicate" and intent["mode"] == "run"
    assert intent["survivor"] and Path(intent["survivor"]).exists()


def test_an_unproven_duplicate_is_kept_past_retention_and_reported(lib, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)
    for p in list(s1.path.iterdir()):                       # 库里那一份后来没了
        if p.suffix == ".mkv":
            p.unlink()

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001", now=_later(31))

    assert moved.exists() and not rep.deleted
    [c] = rep.overdue
    assert c.trash_path == moved and not c.eligible


def test_manual_purge_can_release_a_verified_duplicate_before_retention(lib, no_rmtree):
    """`purge --apply` 是人要的：证明安全的判重不必等满保留期。"""
    s1, good, moved = _trash_the_raw_one(lib)

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert [c.trash_path for c in rep.deleted] == [moved] and not moved.exists()


def test_manual_purge_never_touches_what_needs_a_human(lib, no_rmtree):
    bundle = legacy(lib, "【7月】尼古喵喵 11【TV版】.mp4", rule="duplicate-episode",
                    kind="bundled_version", days_ago=90)
    other = legacy(lib, "x.mkv", rule="-", kind="-", days_ago=90, audit=False)

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001")

    assert bundle.exists() and other.exists() and not rep.deleted


def test_cmd_purge_preview_changes_nothing(lib, monkeypatch, capsys, no_rmtree):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.show("朱音落语").season(1).single("朱音落语 S01E01.mp4", name="[G] Akane - 01.mp4")
    extra = legacy(lib, "尼古喵喵 [Tokuten][01].mkv", rule="extras-in-library", kind="extra",
                   days_ago=40)

    rc = cli.cmd_purge(argparse.Namespace(apply=False, verbose=True, no_tmdb=True), lib.cfg)

    assert rc == 0 and extra.exists() and _wal(lib) == []
    assert "Tokuten" in capsys.readouterr().out
