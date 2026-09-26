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


# ------------------------------------------------------------------ 最短隔离期
def test_manual_purge_keeps_anything_younger_than_the_minimum_quarantine_age(lib, no_rmtree):
    """刚隔离的判重即使证明得了也不删：回退（restore_from_trash）要用它。以前 `purge --apply`
    没有任何年龄下限，一分钟前隔离的也照删——那一批就再也回退不了。"""
    s1, good, moved = _trash_the_raw_one(lib)

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(1))

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert not c.eligible and "最短隔离期" in c.why

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p002", now=_later(3.5))
    assert [c.trash_path for c in rep.deleted] == [moved]


def test_minimum_age_is_configurable_and_also_binds_short_retention(lib, no_rmtree):
    lib.configure(trash_retention_days=1, quarantine_min_age_days=5)
    extra = legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=3)

    [c] = purge.build_pool(lib.context())
    assert c.expired and not c.eligible and "最短隔离期" in c.why

    [c] = purge.build_pool(lib.context(), now=_later(2.5))
    assert c.eligible
    assert extra.exists()


def test_quarantine_min_age_comes_from_the_environment(monkeypatch):
    from media_agent.config import load_config

    assert load_config().quarantine_min_age_days == 3
    monkeypatch.setenv("QUARANTINE_MIN_AGE_DAYS", "7.5")
    assert load_config().quarantine_min_age_days == 7.5


# ------------------------------------------------------------------ 替代者：按检测器的集位解析认
REZERO = "Re：从零开始的异世界生活"
FYY = "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][AVC AAC].mp4"


def _rezero_trashed(lib):
    """2026-08-31：AutoBangumi 把 `3rd Season - 08` 改名成 `S01E08`，与 2016 年真正的第 8 集撞进同一个
    集位，判重把 1.31GB 的原片清进了隔离区。purge 按文件名正则找替代者，会认下这个"S01E08"
    （完整、种子校验通过）并把唯一的原片硬删掉。"""
    return legacy(lib, f"{REZERO} S01E08.mkv", show=REZERO, rule="duplicate-episode",
                  kind="duplicate", days_ago=40,
                  summary=f"S01E08 重复：保留 Re:从零开始的异世界生活 S01E08.mkv，清理 {REZERO} S01E08.mkv")


@pytest.mark.parametrize("category", ["Bangumi", REZERO], ids=["ab-owned", "handed-over"])
def test_a_survivor_whose_release_is_another_season_does_not_vouch(lib, no_rmtree, category):
    moved = _rezero_trashed(lib)
    lib.show(REZERO).season(1).single("Re:从零开始的异世界生活 S01E08.mkv", size=487_000_000,
                                      name=FYY, category=category)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert not c.eligible and c.survivor is not None
    assert ("第 3 季" in c.why) or ("AutoBangumi" in c.why)


def test_a_survivor_pinned_to_another_episode_is_not_in_the_slot(lib, no_rmtree):
    """钉子是抓取器的定论：`ma:S01E58` 的文件哪怕名字叫 S01E08，也不替 S01E08 作保。"""
    moved = _rezero_trashed(lib)
    lib.show(REZERO).season(1).single("Re:从零开始的异世界生活 S01E08.mkv", size=487_000_000,
                                      name=FYY, tags="ma:S01E58")

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert "空的" in c.why


def test_the_rezero_slot_with_its_real_pinned_episode_is_provable(lib, no_rmtree):
    """对照：同一个现场，库里 S01E08 此刻是一份钉着 ma:S01E08 的完整文件——那就证明得了。"""
    moved = _rezero_trashed(lib)
    lib.show(REZERO).season(1).single(f"{REZERO} S01E08.mkv", size=1_410_655_350,
                                      name="[VCB-Studio] Re Zero [08][Ma10p_1080p].mkv",
                                      tags="ma:S01E08")

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [moved]


def test_a_survivor_still_under_its_release_name_is_found(lib, no_rmtree):
    """替代者还没改名（发布名里只有 `- 05`）：文件名正则找不到它，检测器的解析找得到。"""
    moved = legacy(lib, "朱音落语 S01E05.mp4", show="朱音落语", rule="duplicate-episode",
                   kind="duplicate", days_ago=40, summary="S01E05 重复：保留 …，清理 朱音落语 S01E05.mp4")
    surv = lib.show("朱音落语").season(1).single("[Group] Akane-banashi - 05 [1080p].mp4")

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    [c] = rep.pool
    assert c.survivor == surv.path and c.slot == (1, 5)
    assert [c.trash_path for c in rep.deleted] == [moved]


def test_the_slot_comes_from_the_detector_not_the_file_name(lib, no_rmtree):
    """新记录的 `deletion.slot` 是检测器解析过偏移 / 钉子的集位；旧记录的摘要 `S03E01 重复` 也是。
    文件名里的 `- 25` 是发布方的绝对集号（episode_offset -24 的那一季），不能拿来找替代者。"""
    moved = legacy(lib, "[Group] Kanojo 100 - 25 [1080p].mkv", show="女朋友", season="Season 3",
                   rule="duplicate-episode", kind="duplicate", days_ago=40,
                   summary="S03E01 重复：保留 女朋友 S03E01.mkv，清理 [Group] Kanojo 100 - 25 [1080p].mkv",
                   deletion={"gate": "passed", "disposition": "duplicate", "slot": [3, 1]})
    lib.show("女朋友").season(3).single("女朋友 S03E01.mkv", tags="ma:S03E01", probe=CHI,
                                        name="[Other] Kanojo 100 - 25 [1080p].mkv")

    [c] = purge.build_pool(lib.context())

    assert c.slot == (3, 1) and c.eligible and moved.exists()


@pytest.mark.allow("log_failure", match=r"files\(")
def test_duplicates_are_held_when_the_rescan_cannot_see_every_torrent(lib, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)
    other = s1.single("尼古喵喵 S01E06.mkv", name="[G] Yani Neko - 06.mkv")
    lib.qbit.fail("files", hash=other.hash, times=None)

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert moved.exists() and not rep.deleted
    [c] = [c for c in rep.pool if c.trash_path == moved]
    assert "看不全" in c.why


# ------------------------------------------------------------------ 原路径仍被种子声明
# 旧 purge 的 `.!qB` 检查拿**隔离区里的路径**去比种子的条目——没有任何种子指向 state/trash，
# 这道检查永远通过。真正该问的是**当初的路径**：还有种子以优先级非 0 声明它，说明隔离没做完
# （第 1 阶段之前 `qbit.delete` / 设为不下载失败只记一行日志、照样搬文件），或者又有种子要往那写。
def test_an_expired_extra_whose_origin_is_still_wanted_is_kept(lib, no_rmtree):
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"尼古喵喵 S01E01.mkv": GB, "尼古喵喵 NCOP.mkv": 90_000_000},
                      name="[G] Yani Neko 01+NCOP", layout="nosub")
    ncop = s1.path / "尼古喵喵 NCOP.mkv"
    ncop.unlink()                                   # 文件当初被搬走了，种子那一步没做成
    moved = legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=40)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert not c.eligible and pack.hash[:8] in c.why


def test_a_priority_zero_claim_does_not_hold_it(lib, no_rmtree):
    """对照：file_only 隔离之后种子仍列着那个条目、优先级 0——这正是隔离做完了的样子。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.torrent({"尼古喵喵 S01E01.mkv": GB, "尼古喵喵 NCOP.mkv": 90_000_000},
               name="[G] Yani Neko 01+NCOP", layout="nosub", priorities={"尼古喵喵 NCOP.mkv": 0})
    (s1.path / "尼古喵喵 NCOP.mkv").unlink()
    moved = legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=40)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [moved]


def test_the_survivor_sitting_on_the_origin_name_is_not_a_competing_claim(lib, no_rmtree):
    """对照：输家当初就叫 `尼古喵喵 S01E05.mkv`，赢家同一批改名到了这个名字上——原路径被
    替代者自己的种子声明，那正是证明，不是阻碍。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single("[LoliHouse] Yani Neko - 05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
              size=GB - 7, probe=CHI)
    s1.single("尼古喵喵 S01E05.mkv", name="[Raw] Yani Neko - 05 (1080p AVC).mkv",
              size=GB + 100_000_000, probe=RAW)
    c = lib.cycle()
    [rec] = c.applied("trash")
    assert rec["args"]["path"] == str(s1.path / "尼古喵喵 S01E05.mkv")
    assert (s1.path / "尼古喵喵 S01E05.mkv").exists()        # 赢家已经坐上了这个名字
    [moved] = lib.trash_files()

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001", now=_later(31))

    assert [c.trash_path for c in rep.deleted] == [moved]


def test_a_duplicate_its_own_pack_still_wants_is_kept(lib, no_rmtree):
    """第 1 阶段之前：合集里一集判输、`set_file_priority` 失败只记日志，文件照样搬进隔离区——
    合集仍以优先级 1 声明那个路径，替代者再完整也不能删：删了合集就指着一个不存在的文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"[G] Yani Neko - 05.mkv": GB, "[G] Yani Neko - 06.mkv": GB},
                      name="[G] Yani Neko 05-06", layout="nosub", probe=RAW)
    (s1.path / "[G] Yani Neko - 05.mkv").unlink()
    s1.single("尼古喵喵 S01E05.mkv", name="[LoliHouse] Yani Neko - 05 [WebRip 1080p].mkv",
              probe=CHI)
    moved = legacy(lib, "[G] Yani Neko - 05.mkv", rule="duplicate-episode", kind="duplicate",
                   days_ago=40, summary="S01E05 重复：保留 尼古喵喵 S01E05.mkv，清理 [G] Yani Neko - 05.mkv",
                   args={"torrent_hash": pack.hash})

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert c.survivor is not None and pack.hash[:8] in c.why


def test_a_dead_partial_whose_name_a_new_download_wants_is_kept(lib, no_rmtree):
    s1 = lib.show("尼古喵喵").season(1)
    s1.torrent({"尼古喵喵 S01E11.mkv": GB}, name="[B] Yani Neko - 11.mkv", layout="single",
               progress=0.0)
    moved = legacy(lib, "尼古喵喵 S01E11.mkv.!qB", rule="dead-torrent", kind="dead_torrent",
                   days_ago=40)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted


def test_nothing_is_eligible_when_qbittorrent_cannot_be_asked(lib):
    """预演在 qBittorrent 不可用时照样能看，但原路径问不了——一个都不算可删。"""
    legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=40)

    [c] = purge.build_pool(lib.context(qbit=None))

    assert not c.eligible and "无法确认" in c.why


# ------------------------------------------------------------------ I4 复排：按现在的排序它会赢就不删
QIONGLU = "穹庐下的魔女"


def _probed_legacy(lib, spec, name, **kw):
    """带探测结果的隔离文件（FakeProbe 按路径登记）。"""
    p = legacy(lib, name, show=QIONGLU, **kw)
    lib.probe.set(p, spec)
    return p


def test_a_trashed_copy_that_would_now_win_is_kept(lib, no_rmtree):
    """2026-09-08 穹庐下的魔女 S01E11：带两条中文内封字幕的那份按当时的排序输在体积上，被清进
    隔离区，当天人工捞回。排序后来改成先看探到的字幕轨——按现在的规则它才是该留的那份。
    purge 只证明了"库里那份完整"，就会把更好的这份硬删掉。"""
    moved = _probed_legacy(lib, CHI, f"[LoliHouse] {QIONGLU} - 11 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
                           rule="duplicate-episode", kind="duplicate", days_ago=40,
                           summary=f"S01E11 重复：保留 {QIONGLU} S01E11.mkv，清理 [LoliHouse] …",
                           size=GB - 7)
    lib.show(QIONGLU).season(1).single(f"{QIONGLU} S01E11.mkv", size=GB + 100_000_000,
                                       name="[Raw] Qionglu - 11 (1080p AVC).mkv", probe=RAW)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert moved.exists() and not rep.deleted
    [c] = rep.pool
    assert not c.eligible and "排序" in c.why


def test_a_sealed_survivor_is_not_re_ranked(lib, no_rmtree):
    """对照：库里那份钉着这一集、复核通过（封存）——封存不拿画质跟隔离区里的比，与判重同一口径。"""
    moved = _probed_legacy(lib, video("hevc", height=2160, subs=["chi 简体中文"]),
                           f"[BD] {QIONGLU} - 11 [2160p].mkv",
                           rule="duplicate-episode", kind="duplicate", days_ago=40,
                           summary="S01E11 重复：集位已封存，保留 …")
    lib.show(QIONGLU).season(1).single(f"{QIONGLU} S01E11.mkv", tags="ma:S01E11",
                                       name="[LoliHouse] Qionglu - 11 [WebRip 1080p ASSx2].mkv",
                                       probe=CHI)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [moved]


@pytest.mark.parametrize("spec", [CHI, None], ids=["passes-review", "probe-unavailable"])
def test_a_trashed_copy_that_was_itself_sealed_is_kept(lib, no_rmtree, spec):
    """隔离的这份钉着这一集、复核通过（或探测不可用——当作封存，与删除关口 I4 同口径）：
    两份都封存时删哪个由人定。"""
    p = legacy(lib, f"{QIONGLU} S01E11.mkv", show=QIONGLU, rule="duplicate-episode",
               kind="duplicate", days_ago=40, summary="S01E11 重复：…",
               deletion={"gate": "passed", "disposition": "duplicate", "slot": [1, 11],
                         "subject": {"torrent_hash": "a" * 40, "name": "[SubA] Qionglu - 11.mkv",
                                     "pin": "S01E11", "tags": "ma:S01E11", "category": QIONGLU,
                                     "torrent_files": 1}})
    if spec is not None:
        lib.probe.set(p, spec)
    lib.show(QIONGLU).season(1).single(f"{QIONGLU} S01E11 v2.mkv", tags="ma:S01E11",
                                       name="[SubB] Qionglu - 11.mkv", probe=CHI)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert p.exists() and not rep.deleted
    [c] = rep.pool
    assert "封存" in c.why


def test_byte_identical_copies_are_not_re_ranked(lib, no_rmtree):
    """同一份内容（大小 + 头尾摘要相同）谁留都一样，不拿排序挡。"""
    s1 = lib.show(QIONGLU).season(1)
    surv = s1.single(f"{QIONGLU} S01E11.mkv", name="[Raw] Qionglu - 11 (1080p AVC).mkv", probe=RAW)
    p = legacy(lib, "[Better] Qionglu - 11.mkv", show=QIONGLU, rule="duplicate-episode",
               kind="duplicate", days_ago=40, summary="S01E11 重复：字节完全相同")
    import shutil as _sh
    _sh.copyfile(surv.path, p)                       # 同一份字节
    lib.probe.set(p, CHI)                            # 哪怕探测说它"更好"

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert [c.trash_path for c in rep.deleted] == [p]


# ------------------------------------------------------------------ 删之前的最后一问
# 评估（重扫整个库、探测、算摘要）到逐个 unlink 之间可能隔着几分钟；运行锁挡得住别的 media-agent，
# 挡不住 AutoBangumi、qBittorrent、用户。每一个在 unlink 之前按此刻再问一遍。
def _after_planning(monkeypatch, change):
    real = purge.build_pool

    def build_pool(*a, **k):
        pool = real(*a, **k)
        change()
        return pool

    monkeypatch.setattr(purge, "build_pool", build_pool)


def _survivor_path(s1):
    return s1.path / "尼古喵喵 S01E05.mkv"


def test_survivor_gone_after_planning_means_no_delete(lib, monkeypatch, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)
    _after_planning(monkeypatch, lambda: _survivor_path(s1).unlink())

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert moved.exists() and not rep.deleted
    [(c, why)] = rep.changed
    assert c.trash_path == moved and "替代者" in why
    assert _wal(lib) == []                              # 连意图都不写


def test_survivor_torrent_dropped_after_planning_means_no_delete(lib, monkeypatch, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)
    _after_planning(monkeypatch, lambda: lib.qbit.delete([good.hash], delete_files=False))

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert moved.exists() and not rep.deleted and rep.changed


def test_trash_file_changed_after_planning_means_no_delete(lib, monkeypatch, no_rmtree):
    s1, good, moved = _trash_the_raw_one(lib)
    _after_planning(monkeypatch, lambda: moved.write_bytes(b"replaced"))

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert moved.read_bytes() == b"replaced" and not rep.deleted
    [(c, why)] = rep.changed
    assert "隔离文件" in why


def test_origin_claimed_after_planning_means_no_delete(lib, monkeypatch, no_rmtree):
    extra = legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=40)
    s1 = lib.show("尼古喵喵").season(1)
    _after_planning(monkeypatch, lambda: s1.torrent({"尼古喵喵 NCOP.mkv": 90_000_000},
                                                    name="[G] Yani Neko NCOP.mkv",
                                                    layout="single", progress=0.0))

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert extra.exists() and not rep.deleted
    [(c, why)] = rep.changed
    assert "原路径" in why


def test_nothing_changed_still_deletes(lib, monkeypatch, no_rmtree):
    """对照：评估之后什么都没变，照删。"""
    s1, good, moved = _trash_the_raw_one(lib)
    _after_planning(monkeypatch, lambda: None)

    rep = disposal.dispose(lib.context(), mode="manual", run_id="p001", now=_later(5))

    assert [c.trash_path for c in rep.deleted] == [moved] and not rep.changed


# ------------------------------------------------------------------ 容量闸 MIN_FREE_GB
# 隔离区与媒体在同一个 APFS 容器（约 94% 满，critic N8）：隔离不腾空间，只有硬删除腾。时间清理换掉
# 之后，要有东西在空间真紧张时腾出来——但只从"已经证明可以删"的里面挑，最老的先删，够了就停。
def _free(monkeypatch, n):
    monkeypatch.setattr(disposal, "free_bytes", lambda path: n)


def _sealed_dup(lib, ep, days_ago, size=1000):
    """隔离区里一份判重输家（旧记录）+ 库里一份钉着这一集、复核通过的替代者：证明得了。"""
    p = legacy(lib, f"[Raw] Yani Neko - {ep:02d}.mkv", rule="duplicate-episode", kind="duplicate",
               days_ago=days_ago, size=size, summary=f"S01E{ep:02d} 重复：保留 …，清理 …")
    lib.show("尼古喵喵").season(1).single(
        f"尼古喵喵 S01E{ep:02d}.mkv", tags=f"ma:S01E{ep:02d}", probe=CHI,
        name=f"[LoliHouse] Yani Neko - {ep:02d} [WebRip 1080p HEVC-10bit AAC ASSx2].mkv")
    return p


def test_low_space_releases_proven_duplicates_oldest_first_until_above_the_floor(
        lib, monkeypatch, no_rmtree):
    old, mid, new = (_sealed_dup(lib, 5, 20), _sealed_dup(lib, 6, 10), _sealed_dup(lib, 7, 5))
    lib.configure(min_free_gb=50)
    _free(monkeypatch, 50 * 10**9 - 1500)               # 差 1500 字节：删两份（各 1000）就够

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert rep.low_space
    assert [c.trash_path for c in rep.deleted] == [old, mid]      # 最老的先删，够了就停
    assert new.exists()
    modes = [r["mode"] for r in _wal(lib) if r.get("phase") == "intent"]
    assert modes == ["capacity", "capacity"]
    assert all("空间不足" in r["reason"] for r in _wal(lib) if r.get("phase") == "intent")


def test_enough_space_keeps_the_rollback_window(lib, monkeypatch, no_rmtree):
    """对照：空间充足时，保留期内的判重一个不动（回退要用）。"""
    old = _sealed_dup(lib, 5, 20)
    _free(monkeypatch, 10**13)

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert old.exists() and not rep.deleted and not rep.low_space


def test_low_space_never_deletes_anything_unproven(lib, monkeypatch, no_rmtree):
    young = _sealed_dup(lib, 5, 1)                      # 证明得了，但不满最短隔离期
    unproven = legacy(lib, "[Raw] Yani Neko - 09.mkv", rule="duplicate-episode", kind="duplicate",
                      days_ago=20, summary="S01E09 重复：…")          # 库里没有 E09
    extra = legacy(lib, "尼古喵喵 NCOP.mkv", rule="extras-in-library", kind="extra", days_ago=20)
    bundle = legacy(lib, "【7月】尼古喵喵 11【TV版】.mp4", rule="duplicate-episode",
                    kind="bundled_version", days_ago=90)
    other = legacy(lib, "x.mkv", rule="-", kind="-", days_ago=90, audit=False)
    _free(monkeypatch, 0)                               # 一个字节都不剩

    rep = disposal.dispose(lib.context(), mode="run", run_id="p001")

    assert rep.low_space and not rep.deleted
    assert all(p.exists() for p in (young, unproven, extra, bundle, other))


def test_run_output_warns_loudly_when_space_is_low(offline_cli, monkeypatch, capsys, no_rmtree):
    lib = offline_cli
    lib.configure(min_free_gb=50)
    _free(monkeypatch, 12 * 10**9)

    assert cli.cmd_run(_run_args(), lib.cfg) == 0

    cap = capsys.readouterr()
    assert "MIN_FREE_GB" in cap.out and "⚠️" in cap.out
    assert "MIN_FREE_GB" in cap.err                     # stderr（run.err.log）也有一份


def test_min_free_gb_comes_from_the_environment(monkeypatch):
    from media_agent.config import load_config

    assert load_config().min_free_gb == 50
    monkeypatch.setenv("MIN_FREE_GB", "80")
    assert load_config().min_free_gb == 80
