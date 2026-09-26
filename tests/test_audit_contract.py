"""审计状态 `unknown`：改动也许生效了、执行器却确认不了——不许记成 `failed`。

生产 2026-09-16 起 12 次抓取（run 20260916T041844 … 20260926T055214）在加种**成功之后**撞上
`resp` NameError，全记成 `failed`、没有逆操作：外面看是"抓取失败"，qBittorrent 里其实多了种子。
`failed` 的含义必须是"没生效"；"异常发生在已经发出的改动之后"只能说"不知道"。

`unknown` 是向后兼容地引进的：生产 audit.jsonl 约 9.6k 行、好几代格式（2026-09-26 只读核对：
最早 574 行没有 `run_id`、没有 `undo`；4 条回退汇总只有 `ts/run_id/status` 与计数；
2539 条带 `undo`；51 条 failed 带 `error`）。每一个读审计状态的地方都要认得它、不因它崩溃：

- 执行器的报告分桶（critic §3.5：`{"applied","skipped","failed"}[status]` 遇到新状态就 KeyError）；
- `runs` / `rollback` / `repair`：带逆操作的 `unknown` 提供给回退，并明确标出"当初未确认"；
- 隔离区处置：`unknown` 的隔离记录不算"已隔离"，那份文件交给人、写明原因；
- `find_failure_patterns`：反复出现的 `unknown` 同样是信号，单独标出。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

import pytest
from harness import video

from media_agent import cli
from media_agent import purge as purge_mod
from media_agent.actions import Executor
from media_agent.evolution import find_failure_patterns
from media_agent.kernel import Action, Finding

LOLI = "[LoliHouse] Yani Neko - {:02d} [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _finding(op: str, args: dict, **kw) -> Finding:
    return Finding(rule=kw.pop("rule", "t"), kind=kw.pop("kind", "t"), severity="minor",
                   summary=kw.pop("summary", "合成"), action=Action(op=op, args=args), **kw)


def _write(lib, *recs) -> None:
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
        for r in recs:
            fp.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


# ------------------------------------------------------------------ 执行器：分桶与兜底
@pytest.mark.allow("unknown_record")
def test_unknown_status_has_its_own_bucket(lib):
    """critic §3.5：报告分桶是一个只有三个键的 dict，写一条新状态就 KeyError。"""
    ex = Executor(lib.context(), dry_run=False, run_id="t-bucket")
    f = _finding("retag", {"torrent_hash": "a" * 40, "tags": "x"})

    ex._audit("unknown", f, f.action, {"reason": "合成"})

    assert [r["status"] for r in ex.report.unknown] == ["unknown"]
    assert not (ex.report.applied or ex.report.skipped or ex.report.failed)
    assert "未确认 1 项" in ex.report.summary()
    [rec] = lib.audit("t-bucket")
    assert rec["status"] == "unknown"


def test_an_unrecognised_status_lands_in_unknown_instead_of_raising(lib):
    """critic §3.5 的本意：一个不认识的状态（不该出现）不能在改动之后 KeyError。上面那条写的是 `unknown`，
    它本来就在分桶里——把默认桶去掉、换回 `{…}[status]` 全套照绿（复审变异 B1n）。"""
    ex = Executor(lib.context(), dry_run=False, run_id="t-weird")
    f = _finding("retag", {"torrent_hash": "a" * 40, "tags": "x"})

    ex._audit("weird", f, f.action, {"reason": "合成"})

    assert [r["status"] for r in ex.report.unknown] == ["weird"]
    assert not (ex.report.applied or ex.report.skipped or ex.report.failed)
    [rec] = lib.audit("t-weird")
    assert rec["status"] == "weird"


@pytest.mark.allow("unknown_record", match="NameError")
def test_exception_after_an_issued_write_is_unknown_not_failed(lib, monkeypatch):
    """生产 12 次抓取的形态：`add_torrent` 已经成功，之后的代码抛 NameError。

    以前记 failed、没有逆操作。现在执行器知道这个动作已经发出过哪些改动（`effects_attempted`），
    有改动在先的异常一律记 unknown。
    """
    show = lib.show("躲在超市后门抽烟的两人")
    show.season(1)
    title = "[LoliHouse] Super no Ura de Yani Suu Futari - 12 [WebRip 1080p HEVC-10bit AAC]"
    url, h = lib.web.torrent(title)
    f = _finding("grab_episode", {"url": url, "title": title, "show_dir": str(show.path),
                                  "season": 1, "episode": 12, "bangumi_id": None,
                                  "category": show.path.name, "official_title": show.path.name},
                 rule="episode-available", kind="episode_grabbable", show=show.path.name)

    def boom(self, *a, **k):
        raise NameError("name 'resp' is not defined")

    monkeypatch.setattr(Executor, "_rename_grabbed", boom)

    rep = lib.apply([f], run_id="g-bug")

    [rec] = rep.unknown
    assert rec["op"] == "grab_episode" and "NameError" in rec["error"]
    assert rec["effects_attempted"] == ["qbit.add_torrent"]
    assert not rep.failed
    assert lib.qbit.has(h)                                    # 种子确实加进去了


@pytest.mark.allow("failed_record", match="KeyError")
def test_exception_before_any_write_stays_failed(lib):
    """什么改动都还没发出就抛异常：照旧是 failed（没生效是确定的）。"""
    f = _finding("retag", {"tags": "x"})                      # 缺 torrent_hash → KeyError，在 add_tags 之前

    rep = Executor(lib.context(), dry_run=False, run_id="t-pre").apply([f])

    [rec] = rep.failed
    assert "KeyError" in rec["error"] and "effects_attempted" not in rec
    assert not rep.unknown


# ------------------------------------------------------------------ 各代格式混在一起的 audit.jsonl
def _legacy_mix(lib) -> dict:
    """按生产 audit.jsonl 的几代格式合成（2026-09-26 只读核对过的键集合）。"""
    now = datetime.now()
    ts = lambda d: (now - timedelta(days=d)).isoformat(timespec="seconds")
    r = {
        # 第 0 代（2026-08-17 上午）：没有 run_id、没有 undo
        "g0": {"ts": ts(40), "status": "applied", "dry_run": False, "rule": "category-consolidation",
               "kind": "category", "op": "retag", "args": {"torrent_hash": "a" * 40, "tags": "x"},
               "summary": "第 0 代"},
        "g0skip": {"ts": ts(40), "status": "skipped", "dry_run": False, "rule": "r", "kind": "k",
                   "op": "recategorize", "args": {}, "summary": "第 0 代", "reason": "dry-run"},
        # 第 1 代：秒级 run_id、带 undo；failed 带 error；没有 undo 的 applied（write_nfo）
        "g1a": {"ts": ts(5), "run_id": "20260921T101010", "status": "applied", "dry_run": False,
                "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename", "args": {},
                "summary": "第 1 代", "undo": {"op": "rename", "path": "/x/y.mkv", "new_name": "z.mkv",
                                             "torrent_hash": ""}},
        "g1f": {"ts": ts(5), "run_id": "20260921T101010", "status": "failed", "dry_run": False,
                "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename", "args": {},
                "summary": "第 1 代", "error": "ReadTimeout: timed out"},
        "g1nfo": {"ts": ts(5), "run_id": "20260921T101010", "status": "applied", "dry_run": False,
                  "rule": "tmdb", "kind": "nfo", "op": "write_nfo", "args": {}, "summary": "nfo"},
        "g1f2": {"ts": ts(3), "run_id": "20260923T101010", "status": "failed", "dry_run": False,
                 "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename", "args": {},
                 "summary": "第 1 代", "error": "ReadTimeout: timed out"},
        "dry": {"ts": ts(3), "run_id": "20260923T111111", "status": "applied", "dry_run": True,
                "rule": "r", "kind": "k", "op": "rename", "args": {}, "summary": "预演",
                "undo": {"op": "rename"}},
        # 历史回退汇总：只有 ts / run_id / status 与计数，run_id 是被回退的那一批
        "rb": {"ts": ts(4), "run_id": "20260921T101010", "status": "rollback", "total": 3,
               "reverted": 1, "skipped": 0, "failed": 0, "irreversible": 1,
               "torrent_records_lost": 0, "skipped_detail": [], "failed_detail": []},
        # 第 3 阶段：带 seq；unknown（一条带逆操作、一条没有）
        "u1": {"ts": ts(2), "run_id": "20260924T090000.001-42", "seq": 1, "status": "unknown",
               "dry_run": False, "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename",
               "args": {}, "summary": "新", "error": "ReadTimeout: timed out",
               "reason": "改名请求超时，此刻读不到 qBittorrent，无法确认",
               "undo": {"op": "rename", "path": "/x/a.mkv", "new_name": "b.mkv", "torrent_hash": ""}},
        "u2": {"ts": ts(2), "run_id": "20260924T090000.001-42", "seq": 2, "status": "unknown",
               "dry_run": False, "rule": "grab", "kind": "grab", "op": "grab_episode", "args": {},
               "summary": "新", "error": "NameError: resp", "effects_attempted": ["qbit.add_torrent"]},
        "u3": {"ts": ts(1), "run_id": "20260925T090000.001-42", "seq": 1, "status": "unknown",
               "dry_run": False, "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename",
               "args": {}, "summary": "新", "error": "ReadTimeout: timed out", "reason": "…"},
        # 将来的状态：读的一方要容忍（不计入任何已知桶，不崩）
        "future": {"ts": ts(1), "run_id": "20260925T090000.001-42", "seq": 2, "status": "partial",
                   "dry_run": False, "rule": "r", "kind": "k", "op": "rename", "args": {},
                   "summary": "将来"},
    }
    _write(lib, r["g0"], r["g0skip"], "{半行坏 JSON", r["g1a"], r["g1f"], r["g1nfo"], "[1, 2]",
           '"字符串"', "", r["rb"], r["g1f2"], r["dry"], r["u1"], r["u2"], r["u3"], r["future"])
    return r


def test_list_runs_reads_every_generation_and_labels_unknown(lib):
    _legacy_mix(lib)

    runs = {r["run_id"]: r for r in Executor(lib.context()).list_runs()}

    assert set(runs) == {"20260921T101010", "20260923T101010", "20260924T090000.001-42",
                         "20260925T090000.001-42"}            # 没有 run_id 的、预演的不成批次
    old = runs["20260921T101010"]
    assert (old["applied"], old["undoable"], old["unconfirmed"]) == (2, 1, 0)
    assert old["rolled_back"] is True                          # 历史回退汇总照旧标记被回退的那一批
    new = runs["20260924T090000.001-42"]
    assert (new["applied"], new["unconfirmed"], new["undoable"]) == (0, 2, 1)   # 带逆操作的 unknown 可回退
    assert not new.get("rolled_back")


def test_cmd_runs_and_rollback_last_offer_unconfirmed_runs(lib, monkeypatch, capsys):
    _legacy_mix(lib)
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())

    assert cli.cmd_runs(argparse.Namespace(), lib.cfg) == 0
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if x.startswith("20260924T090000.001-42")]
    assert "未确认 2" in line
    [line] = [x for x in out.splitlines() if x.startswith("20260921T101010")]
    assert "已回退" in line

    # --last：最近一个"没回退过、有可回退记录"的批次。20260925 的 unknown 没有逆操作，不算
    rc = cli.cmd_rollback(argparse.Namespace(run=None, last=True, dry_run=True), lib.cfg)
    out = capsys.readouterr().out
    assert rc == 0 and "20260924T090000.001-42" in out


def test_find_failure_patterns_reports_repeated_unknown_separately(lib):
    _legacy_mix(lib)

    pats = find_failure_patterns(lib.cfg.audit_log)

    by = {(p["op"], p["status"]): p for p in pats}
    assert by[("rename", "failed")]["runs"] == 2               # 20260921 与 20260923 两批
    assert by[("rename", "unknown")]["runs"] == 2              # 20260924 与 20260925 两批
    assert ("grab_episode", "unknown") not in by               # 只出现过一批，不算"反复"


def test_read_audit_keeps_legacy_file_order_and_sorts_new_by_seq(lib):
    r = _legacy_mix(lib)
    # 同一批的记录分在主审计与备用文件里、文件顺序与写入顺序相反（磁盘满的那一轮）
    fb = lib.cfg.audit_log.with_name("audit.fallback.jsonl")
    late = {**r["u1"], "seq": 0, "summary": "其实最先写"}
    fb.write_text(json.dumps(late, ensure_ascii=False) + "\n", encoding="utf-8")
    ex = Executor(lib.context())

    old = ex._read_audit("20260921T101010")
    assert [x.get("op") or x["status"] for x in old] == ["rename", "rename", "write_nfo", "rollback"]
    new = ex._read_audit("20260924T090000.001-42")
    assert [x["seq"] for x in new] == [0, 1, 2]


# ------------------------------------------------------------------ 回退：带逆操作的 unknown，明确标出
@pytest.mark.allow("unknown_record")
def test_rollback_attempts_unknown_records_and_labels_them(lib, monkeypatch, capsys):
    """一条改名当初记 unknown（请求超时、确认不了）。回退照样按此刻状态去还原它，并说明它当初未确认；
    另一条 unknown 的改动其实没生效（目标名不在），回退按状态核对后跳过、一个字不动。"""
    s1 = lib.show("尼古喵喵").season(1)
    t8 = s1.single(LOLI.format(8), size=593_601_176, probe=SUBS)
    lib.qbit.rename_file(t8.hash, LOLI.format(8), "尼古喵喵 S01E08.mkv")      # 生效了的那条
    t9 = s1.single(LOLI.format(9), size=593_601_177, probe=SUBS)            # 没生效的那条
    base = {"ts": "2026-09-14T10:02:14", "run_id": "20260914T100214", "dry_run": False,
            "rule": "unrenamed-file", "kind": "unrenamed", "op": "rename", "summary": "改名",
            "error": "ReadTimeout: timed out", "reason": "改名请求超时，无法确认"}
    _write(lib,
           {**base, "seq": 1, "status": "unknown", "args": {"path": str(t8.path)},
            "undo": {"op": "rename", "path": str(s1.path / "尼古喵喵 S01E08.mkv"),
                     "new_name": LOLI.format(8), "torrent_hash": t8.hash}},
           {**base, "seq": 2, "status": "unknown", "args": {"path": str(t9.path)},
            "undo": {"op": "rename", "path": str(s1.path / "尼古喵喵 S01E09.mkv"),
                     "new_name": LOLI.format(9), "torrent_hash": t9.hash}})
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())

    rc = cli.cmd_rollback(argparse.Namespace(run="20260914T100214", last=False, dry_run=False),
                          lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0
    assert lib.qbit.file_names(t8.hash) == [LOLI.format(8)]               # 生效了的：还原
    assert lib.qbit.file_names(t9.hash) == [LOLI.format(9)]               # 没生效的：不动
    assert "未确认" in out and "2" in out
    res = Executor(lib.context()).list_runs()
    assert next(r for r in res if r["run_id"] == "20260914T100214")["rolled_back"]


# ------------------------------------------------------------------ 隔离区处置：unknown 的隔离不算数
def test_quarantined_file_whose_trash_record_is_unknown_is_left_to_a_human(lib):
    """搬进隔离区那一步当初没能确认（跨卷先拷后删，半路出错、两边都有）：这份文件可能是不完整的拷贝，
    也可能库里那份才是残的。它不算"已隔离"，过了保留期也不自动删，理由写明。"""
    ts = datetime.now() - timedelta(days=60)
    origin = lib.media_root / "尼古喵喵" / "Season 1" / "尼古喵喵 S01E05.mkv"
    p = lib.cfg.trash_dir / f"{ts:%Y-%m-%d}" / "尼古喵喵" / "Season 1" / origin.name
    p.parent.mkdir(parents=True)
    p.write_bytes(b"partial copy")
    _write(lib, {"ts": ts.isoformat(timespec="seconds"), "run_id": "20260801T000000.000-1",
                 "seq": 1, "status": "unknown", "dry_run": False, "rule": "extras-in-library",
                 "kind": "extras", "op": "trash", "args": {"path": str(origin)}, "summary": "特典",
                 "trashed_to": str(p), "error": "OSError: [Errno 28] No space left on device",
                 "reason": "搬运半路出错，隔离区与原位置都有这个文件",
                 "undo": {"op": "restore_from_trash", "path": str(origin), "trash_path": str(p),
                          "torrent_record_lost": False}})

    [c] = purge_mod.build_pool(lib.context())

    assert not c.eligible
    assert "未确认" in c.why and "需人工处置" in c.why
    assert c.origin == str(origin)


# ------------------------------------------------------------------ repair：unknown 的目录改名同样是分裂现场
def test_repair_considers_unknown_rename_show_dir(lib):
    old = lib.show("旧名").season(1)
    old.local("旧名 S01E01.mkv", size=1000)
    new = lib.show("新名").season(1)
    new.local("新名 S01E02.mkv", size=1000)
    _write(lib, {"ts": "2026-09-20T00:00:00", "run_id": "r-u", "seq": 1, "status": "unknown",
                 "dry_run": False, "rule": "title-drift", "kind": "title_drift",
                 "op": "rename_show_dir", "args": {}, "summary": "x",
                 "error": "OSError: …", "reason": "种子都搬了，残留文件搬到一半出错",
                 "undo": {"op": "rename_show_dir", "path": str(new.show.path), "new_name": "旧名"}})
    lib.configure(qbit_allow_empty=True)

    res = Executor(lib.context(), dry_run=True, run_id="rp").repair_split_dirs("r-u")

    assert res["pairs"] == 1
