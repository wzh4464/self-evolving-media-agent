"""一轮之内收敛（`media_agent/converge.py`）：扫描 → 诊断 → 执行，重复到不动点（最多 MAX_ITERATIONS 次）。

以前一轮 `run` 是"先全量诊断、再统一执行"，前后依赖的两步永远隔一轮（6 小时）：AutoBangumi 的重复版本落在
`Bangumi` 分类，这一轮只能交接分类，判重与赢家的改名要等下一轮（runloop 调研 §8a：生产最近 3 批「集位被占」都与
同批的 recategorize 同时出现）；抓取之后元数据没及时到，改名"下一轮会补"。

这里用 `lib.loop()`（`converge.run`，一个 Context、一个执行器）跑一轮；断言落在外部可观察的
结果上：磁盘、qBittorrent、这一轮的审计。
"""
from __future__ import annotations

import pytest

from harness import MikanItem, video, weekly
from media_agent import converge
from media_agent.kernel import Action, Finding

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
LOLI_09 = "[LoliHouse] Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_09 = "[Dynamis One] Yani Neko - 09 (ABEMA 1920x1080 AVC AAC MKV).mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
SHOW = "尼古喵喵"


def _ab_duplicate(lib, ep: int = 8, loli: str = LOLI_08, abema: str = ABEMA_08):
    """已交接的旧版本（ABEMA 生肉）占着集位名；AutoBangumi 又下了带双字幕的 LoliHouse，还挂在 `Bangumi` 分类下。"""
    s1 = lib.show(SHOW).season(1)
    raw = s1.single(f"{SHOW} S01E{ep:02d}.mkv", size=745_065_995 + ep, name=abema, probe=video("h264"))
    sub = s1.single(loli, size=593_601_176 + ep, probe=TWO_SUBS, category="Bangumi")
    return s1, raw, sub


def _ops(recs: list[dict]) -> list[str]:
    return [r["op"] for r in recs]


# ------------------------------------------------------------------ (a) 交接 → 判重 → 改名，一轮完成
def test_ab_duplicate_in_bangumi_is_handed_over_deduped_and_renamed_in_one_run(lib):
    s1, raw, sub = _ab_duplicate(lib)
    sub_ident = lib.ident(sub.path)

    c = lib.loop()

    applied = _ops(c.applied())
    assert "recategorize" in applied and "trash" in applied and "rename" in applied
    assert lib.disk() == {f"{SHOW}/Season 1/{SHOW} S01E08.mkv": 593_601_184}
    assert lib.ident(s1.path / f"{SHOW} S01E08.mkv") == sub_ident          # 留下的是 LoliHouse
    assert lib.qbit.torrent(sub.hash)["category"] == SHOW
    assert not lib.qbit.has(raw.hash)
    # 同一个批次 ID：整轮仍是一个回退单元
    assert {r["run_id"] for r in lib.audit()} == {c.run_id}
    assert c.outcome.stop == converge.FIXED_POINT
    assert [it.applied > 0 for it in c.iterations] == [True, True, False]
    # 最后一次诊断就是这一轮结束时的样子：没有要做的了
    assert not [f for f in c.findings if f.action]


def test_the_occupied_slot_skip_is_retried_after_the_dedupe_frees_it(lib):
    """「集位被占」是"此刻被挡着"：第一次迭代跳过，判重在第二次迭代腾出集位后再试就成（`converge.RETRYABLE`）。"""
    _ab_duplicate(lib)

    c = lib.loop()

    renames = [r for r in lib.audit(c.run_id) if r["op"] == "rename"]
    assert [r["status"] for r in renames] == ["skipped", "applied"]
    assert renames[0]["reason"].startswith("集位被占")


def test_the_same_library_takes_two_runs_without_the_loop(lib):
    """对照：只跑一次（旧的 `run`）时第一轮只交接了分类。"""
    _ab_duplicate(lib)

    c = lib.loop(max_iterations=1)

    assert _ops(c.applied()) == ["recategorize", "delete_category", "write_sidecar"]
    assert c.outcome.stop == converge.CAP
    assert sorted(f.action.op for f in c.outcome.pending) == ["rename", "trash"]


# ------------------------------------------------------------------ (b) 抓取 → 改名，一轮完成
def test_grab_whose_metadata_arrives_late_is_renamed_in_the_same_run(lib):
    """抓取加种后等元数据没等到（磁力 / 连不上 peer），以前改名"下一轮会补"（2026-09-03 最新三集刮削失败：
    文件在发布名上躺了六小时）。现在第二次迭代的 unrenamed-file 就把它改好。"""
    lib.configure(qbit_allow_empty=True, grab_metadata_timeout=0)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=SHOW, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    item = MikanItem(title=title, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=[SHOW])
    # 加种之后第一次读文件列表失败：元数据这一刻还没到
    lib.qbit.fail("files", hash=item.infohash, times=1)

    c = lib.loop()

    [grab] = c.applied("grab_episode")
    assert grab["metadata"]["outcome"] == "timeout"
    [ren] = c.applied("rename")
    assert ren["args"]["torrent_hash"] == item.infohash
    assert lib.qbit.file_names(item.infohash) == [f"{SHOW} S01E09.mkv"]
    assert len(c.iterations) >= 2 and c.outcome.stop == converge.FIXED_POINT


# ------------------------------------------------------------------ (c) 两条规则打架
class _Flip:
    """合成的一对规则：一条把 `a` 改成 `b`，另一条把 `b` 改回 `a`（B5 那种来回改名，压进了一轮）。"""

    def __init__(self, rule: str, src: str, dst: str):
        self.id, self.src, self.dst = rule, src, dst

    def detect(self, ctx, state):
        for show in state.shows:
            for f in show.files:
                if f.filename == self.src:
                    yield Finding(rule=self.id, kind="flip", severity="minor",
                                  summary=f"{self.src} → {self.dst}", show=show.dir_name,
                                  path=str(f.path), torrent_hash=f.torrent_hash,
                                  action=Action(op="rename", args={
                                      "path": str(f.path), "new_name": self.dst,
                                      "torrent_hash": f.torrent_hash}))


class _Recat:
    def __init__(self, rule: str, src: str, dst: str):
        self.id, self.src, self.dst = rule, src, dst

    def detect(self, ctx, state):
        for t in state.torrents:
            if t.get("category") == self.src:
                yield Finding(rule=self.id, kind="recat", severity="minor",
                              summary=f"分类 {self.src} → {self.dst}", torrent_hash=t["hash"],
                              evidence={"current": self.src},
                              action=Action(op="recategorize",
                                            args={"torrent_hash": t["hash"], "category": self.dst}))


class _Chain:
    """每次迭代都有一件新事：`x1.mkv` → `x2.mkv` → …（本地文件）。"""
    id = "chain"

    def detect(self, ctx, state):
        for show in state.shows:
            for f in show.files:
                if f.filename.startswith("x") and f.filename[1:-4].isdigit():
                    n = int(f.filename[1:-4])
                    yield Finding(rule=self.id, kind="chain", severity="minor", summary=f.filename,
                                  show=show.dir_name, path=str(f.path),
                                  action=Action(op="rename", args={
                                      "path": str(f.path), "new_name": f"x{n + 1}.mkv", "torrent_hash": ""}))


def test_rename_back_in_the_same_run_is_refused_and_reported(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single("a.mkv")

    c = lib.loop(detectors=[_Flip("rule-a", "a.mkv", "b.mkv"), _Flip("rule-b", "b.mkv", "a.mkv")])

    assert lib.qbit.file_names(t.hash) == ["b.mkv"]                       # 只改了一次
    [osc] = [f for f in c.findings if f.kind == "oscillation"]
    assert osc.rule == "run-loop" and osc.severity == "important"
    assert osc.evidence["first"]["rule"] == "rule-a" and osc.evidence["second"]["rule"] == "rule-b"
    assert osc.evidence["first"]["iteration"] == 1 and osc.evidence["second"]["iteration"] == 2
    [refused] = [r for r in lib.audit(c.run_id) if r["status"] == "skipped"]
    assert refused["rule"] == "rule-b" and refused["reason"].startswith("反向动作")
    assert refused["reverses"]["rule"] == "rule-a"
    assert c.outcome.stop == converge.FIXED_POINT
    assert [it.reversed for it in c.iterations] == [0, 1]


def test_category_flip_back_in_the_same_run_is_refused(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single(f"{SHOW} S01E01.mkv", category="甲")

    c = lib.loop(detectors=[_Recat("rule-a", "甲", "乙"), _Recat("rule-b", "乙", "甲")])

    assert lib.qbit.torrent(t.hash)["category"] == "乙"
    assert [f.kind for f in c.findings if f.rule == "run-loop"] == ["oscillation"]


def test_an_oscillation_is_reported_once_even_when_it_keeps_coming_back(lib):
    s1 = lib.show(SHOW).season(1)
    s1.single("a.mkv")
    s1.local("x1.mkv")

    c = lib.loop(detectors=[_Flip("rule-a", "a.mkv", "b.mkv"), _Flip("rule-b", "b.mkv", "a.mkv"), _Chain()],
                 max_iterations=4)

    assert len([f for f in c.findings if f.kind == "oscillation"]) == 1
    assert len([r for r in lib.audit(c.run_id) if r.get("reason", "").startswith("反向动作")]) == 1


# ------------------------------------------------------------------ (d) 配额跨迭代累计
def test_delete_quota_is_cumulative_across_iterations(lib):
    """一轮一个执行器：`MAX_DELETE_PER_RUN` 管的是整轮，不是每次迭代——每次迭代新建执行器就成了 N 倍。"""
    lib.configure(max_delete_per_run=1)
    s1 = lib.show(SHOW).season(1)
    # 第 8 集：两份都已交接，第一次迭代就判重（用掉唯一的配额）
    s1.single(f"{SHOW} S01E08.mkv", size=745_065_995, name=ABEMA_08, probe=video("h264"))
    s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)
    # 第 9 集：新版本还在 `Bangumi` 下，第二次迭代才轮到判重
    _s, raw9, _sub9 = _ab_duplicate(lib, ep=9, loli=LOLI_09, abema=ABEMA_09)

    c = lib.loop()

    trashes = [r for r in lib.audit(c.run_id) if r["op"] == "trash"]
    assert [r["status"] for r in trashes] == ["applied", "skipped"]
    assert trashes[1]["reason"] == "已达单轮删除数量上限 1"
    assert trashes[1]["args"]["path"].endswith(f"{SHOW} S01E09.mkv")
    assert lib.qbit.has(raw9.hash) and len(lib.trash_files()) == 1
    # 被配额拦下的不在后面的迭代里再试（`converge` 的"本轮已试过"）
    assert len(trashes) == 2


# ------------------------------------------------------------------ 不重试
class _Once:
    """每次迭代都提同一个动作。"""

    def __init__(self, op: str, args: dict, rule: str = "synthetic"):
        self.id, self.op, self.args = rule, op, args

    def detect(self, ctx, state):
        yield Finding(rule=self.id, kind=f"synthetic-{self.op}", severity="minor", summary=f"{self.op} 合成",
                      path=str(self.args.get("path", "")), torrent_hash=self.args.get("torrent_hash", ""),
                      action=Action(op=self.op, args=dict(self.args)))


@pytest.mark.allow("failed_record", match="种子文件列表里找不到该文件")
def test_a_failed_action_is_not_retried_in_the_same_run(lib):
    """以前把执行放进循环，每次迭代都会再撞一次同样的失败（B2 的 404 改名）、多写一条 failed 审计。"""
    s1 = lib.show(SHOW).season(1)
    t = s1.single(LOLI_08, probe=TWO_SUBS)
    ghost = s1.path / "不在种子里.mkv"
    fail = _Once("rename", {"path": str(ghost), "new_name": "x.mkv", "torrent_hash": t.hash})

    c = lib.loop(detectors=[fail, *lib.registry().detectors])

    failed = [r for r in lib.audit(c.run_id) if r["status"] == "failed"]
    assert len(failed) == 1                        # 第二次迭代（改名、写档案之后）没再试
    assert len(c.iterations) >= 2
    assert c.iterations[1].memo >= 1


def test_an_applied_action_proposed_again_is_not_repeated(lib):
    """执行过的动作第二次迭代又被提出来（状态没落地、或规则没看见自己的结果）：不再做第二遍。"""
    s1 = lib.show(SHOW).season(1)
    t = s1.single(f"{SHOW} S01E01.mkv")
    tag = _Once("retag", {"torrent_hash": t.hash, "tags": "x"})
    other = _Once("recategorize", {"torrent_hash": t.hash, "category": "乙"}, rule="other")

    c = lib.loop(detectors=[tag, other])

    assert _ops(c.applied()) == ["retag", "recategorize"]
    assert c.outcome.stop == converge.FIXED_POINT and len(c.iterations) == 2


# ------------------------------------------------------------------ 到顶
def test_reaching_the_cap_reports_what_the_next_iteration_would_do(lib):
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    s1.local("x1.mkv")

    c = lib.loop(detectors=[_Chain()], max_iterations=3)

    assert c.outcome.stop == converge.CAP
    assert [it.applied for it in c.iterations] == [1, 1, 1]
    final = c.outcome.iterations[-1]
    assert final.final and final.applied == 0
    [p] = c.outcome.pending
    assert p.action.args["new_name"] == "x5.mkv"
    assert lib.disk() == {f"{SHOW}/Season 1/x4.mkv": 600_000_000}
    # 收尾诊断不执行、不写审计
    assert len(lib.audit(c.run_id)) == 3


def test_cap_reached_exactly_at_the_fixed_point_is_not_reported_as_pending(lib):
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    s1.local("x1.mkv")

    class Upto3(_Chain):
        def detect(self, ctx, state):
            return [f for f in super().detect(ctx, state) if f.summary != "x3.mkv"]

    c = lib.loop(detectors=[Upto3()], max_iterations=2)

    assert c.outcome.stop == converge.FIXED_POINT and c.outcome.pending == []


# ------------------------------------------------------------------ 预演 / 一个执行器的跨迭代状态
def test_dry_run_iterates_once(lib):
    _ab_duplicate(lib)

    c = lib.loop(dry_run=True)

    assert len(c.iterations) == 1 and c.outcome.stop == converge.DRY_RUN
    assert not c.applied()


def test_a_path_trashed_in_one_iteration_can_hold_the_keeper_in_the_next(lib):
    """"本批次已隔离"按路径记（改名与删除关口据此拒绝）。第一次迭代把输家从集位名上移走、赢家改了过去；第二次迭代
    里集位名上是赢家——它得能当保留方。以前的一个执行器跨迭代会记着"这个路径已隔离"，关口据此拒绝：
    「保留方 … 本批次已被移进隔离区」。"""
    s1 = lib.show(SHOW).season(1)
    s1.single(f"{SHOW} S01E08.mkv", size=745_065_995, name=ABEMA_08, probe=video("h264"))
    sub = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)
    extra = s1.local("另一份.mkv", size=500_000_000, same_content_as=sub.path)
    slot = s1.path / f"{SHOW} S01E08.mkv"

    class ThirdCopy:
        """集位名上已是赢家时，清掉同内容的另一份（点名赢家当保留方）。"""
        id = "third-copy"

        def detect(self, ctx, state):
            keeper = next((f for s in state.shows for f in s.files
                           if f.path == slot and f.torrent_hash == sub.hash), None)
            if keeper is None or not extra.exists():
                return
            yield Finding(rule=self.id, kind="duplicate", severity="important", summary="另一份",
                          show=SHOW, path=str(extra),
                          action=Action(op="trash", args={
                              "path": str(extra), "torrent_hash": "", "slot": [1, 8],
                              "keep_path": str(slot), "keep_hash": sub.hash,
                              "keep_size": keeper.size, "keep_digest": ""}))

    c = lib.loop(detectors=[ThirdCopy(), *lib.registry().detectors])

    trashes = [r for r in lib.audit(c.run_id) if r["op"] == "trash"]
    assert [r["status"] for r in trashes] == ["applied", "applied"], [r.get("reason") for r in trashes]
    assert not extra.exists()


def test_key_of_names_the_target_not_the_whole_payload():
    a = converge.key_of("write_sidecar", {"show_dir": "/m/甲", "payload": {"x": 1}})
    b = converge.key_of("write_sidecar", {"show_dir": "/m/甲", "payload": {"x": 2}})
    assert a == b
    assert converge.key_of("rename", {"path": "/m/a.mkv", "torrent_hash": "h1", "new_name": "b"}) != \
        converge.key_of("rename", {"path": "/m/a.mkv", "torrent_hash": "h2", "new_name": "b"})
    # 不认识的动作按全部参数认
    assert converge.key_of("mystery", {"a": 1}) != converge.key_of("mystery", {"a": 2})


def test_undoes_matches_each_recorded_inverse():
    ren = {"status": "applied", "op": "rename", "args": {"path": "/m/a.mkv", "new_name": "b.mkv"},
           "undo": {"op": "rename", "path": "/m/b.mkv", "new_name": "a.mkv", "torrent_hash": ""}}
    assert converge.undoes("rename", {"path": "/m/b.mkv", "new_name": "a.mkv"}, ren)
    assert not converge.undoes("rename", {"path": "/m/b.mkv", "new_name": "c.mkv"}, ren)
    grab = {"status": "applied", "op": "grab_episode", "args": {},
            "undo": {"op": "ungrab_episode", "infohash": "ABC123"}}
    assert converge.undoes("drop_torrent", {"torrent_hash": "abc123"}, grab)
    assert converge.undoes("trash", {"path": "/m/x.mkv", "torrent_hash": "abc123"}, grab)
    dropped = {"status": "applied", "op": "drop_torrent", "args": {"torrent_hash": "a" * 40},
               "undo": {"op": "readd_torrent", "magnet": "magnet:?xt=urn:btih:" + "a" * 40}}
    url = f"https://mikanani.me/Download/20260901/{'a' * 40}.torrent"
    assert converge.undoes("grab_episode", {"url": url}, dropped)
    trashed = {"status": "applied", "op": "trash", "args": {"torrent_hash": "a" * 40},
               "undo": {"op": "restore_from_trash", "torrent_record_lost": False}}
    assert not converge.undoes("grab_episode", {"url": url}, trashed)   # 只作废了一个文件，种子还在


def test_iteration_line_is_one_compact_line():
    it = converge.Iteration(2, findings=5, actionable=3, attempted=2, memo=1, applied=2, scan_s=1.2)
    line = it.line(3)
    assert "\n" not in line and "迭代 2/3" in line and "执行 2" in line and "本轮已试过 1" in line


# ------------------------------------------------------------------ 读不全
def _scan_failing_from(lib, ctx, n_bad: int, h: str):
    from media_agent.scan import build_state

    def scan(n):
        if n >= n_bad:
            lib.qbit.fail("files", hash=h, times=None)
        return build_state(ctx)
    return scan


@pytest.mark.allow("log_failure", match="qBittorrent 数据不完整")
def test_a_degraded_scan_in_a_later_iteration_stops_the_loop_and_refuses(lib):
    from media_agent.actions import Executor
    s1 = lib.show(SHOW).season(1)
    t = s1.single(LOLI_08, probe=TWO_SUBS)
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")

    out = converge.run(ctx, lib.registry(), ex, scan=_scan_failing_from(lib, ctx, 2, t.hash),
                       max_iterations=3)

    assert out.stop == converge.REFUSED and "files(" in out.refused
    assert [it.refused != "" for it in out.iterations] == [False, True]
    assert [r["op"] for r in lib.audit("t001")] == ["rename"]      # 第一次迭代做的；第二次一件没做
    assert out.state.qbit_errors                                                   # 最后一次扫描就是读不全的那一次


@pytest.mark.allow("log_failure", match="qBittorrent 数据不完整")
def test_a_degraded_final_pass_lists_no_pending(lib):
    from media_agent.actions import Executor
    s1 = lib.show(SHOW).season(1)
    t = s1.single(f"{SHOW} S01E01.mkv")
    s1.local("x1.mkv")
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")

    out = converge.run(ctx, lib.registry([_Chain()]), ex, scan=_scan_failing_from(lib, ctx, 2, t.hash),
                       max_iterations=1)

    assert out.stop == converge.CAP and out.pending == []
    assert "files(" in out.final_degraded
