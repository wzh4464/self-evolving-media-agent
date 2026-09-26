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


class _TrashLocal:
    """合成的：每次诊断都提议把 `junk-*.mkv`（纯本地、名字认不出集号）移进隔离区。"""
    id = "junk"

    def detect(self, ctx, state):
        for show in state.shows:
            for f in show.files:
                if f.filename.startswith("junk-"):
                    yield Finding(rule=self.id, kind="junk", severity="minor", summary=f.filename,
                                  show=show.dir_name, path=str(f.path),
                                  action=Action(op="trash", args={"path": str(f.path), "torrent_hash": ""}))


def test_a_refused_action_is_not_retried_while_other_work_keeps_the_loop_going(lib):
    """"被闸拦下的，后面的迭代不再试"：上面那条配额测试里第二次迭代什么都没做成、循环就停了，重试不重试根本测不出来
    （2026-09-27 审查：变异"所有跳过都当成可重试"全套存活）。这里另一条规则每次迭代都有新动作，循环跑满三次。"""
    lib.configure(qbit_allow_empty=True, max_delete_per_run=1)
    s1 = lib.show(SHOW).season(1)
    s1.local("x1.mkv")
    s1.local("junk-a.mkv")
    s1.local("junk-b.mkv")

    class Upto4(_Chain):
        def detect(self, ctx, state):
            return [f for f in super().detect(ctx, state) if f.summary != "x4.mkv"]

    c = lib.loop(detectors=[Upto4(), _TrashLocal()], max_iterations=3)

    assert [it.applied for it in c.iterations] == [2, 1, 1]
    trashes = [(r["status"], r["args"]["path"].rsplit("/", 1)[-1]) for r in lib.audit(c.run_id)
               if r["op"] == "trash"]
    assert trashes == [("applied", "junk-a.mkv"), ("skipped", "junk-b.mkv")]    # 只拦一次，不每次迭代再撞一次
    assert [it.memo for it in c.iterations if not it.final] == [0, 1, 1]


# ------------------------------------------------------------------ 不重试
def test_a_relink_blocked_by_a_live_torrent_is_retried_once_that_torrent_is_gone(lib):
    """`RETRYABLE` 的第二条：relink 的目标文件此刻归另一个活种子，"此刻被挡着"——那个种子在这一轮里没了（判重、死种
    处置摘掉），下一次迭代再试就成（2026-09-27 审查：把这句前缀改掉的变异全套存活）。"""
    from media_agent.actions import Executor
    from media_agent.plugins.builtin import StaleTorrentPathDetector
    from media_agent.scan import build_state
    size = 734_003_200
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("古诺希亚").season(1)
    t = s1.single("GNOSIA - S01E08 [WebRip 1080p HEVC-10bit AAC].mkv", size=size, on_disk=False)
    b = s1.single("古诺希亚 S01E08.mkv", size=size, name="[B] Gnosia - 08 [1080p].mkv")
    s1.local("x1.mkv")                                           # 让第一次迭代做成点什么，循环才会再来一次
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")

    def scan(n):
        if n == 2:
            lib.qbit.delete([b.hash], False)                     # 占着的那个种子这一轮被摘了（文件还在）
        return build_state(ctx)

    out = converge.run(ctx, lib.registry([StaleTorrentPathDetector(), _Upto3()]), ex, scan=scan,
                       max_iterations=3)

    relinks = [(r["status"], converge.retryable(r)) for r in lib.audit("t001") if r["op"] == "relink_torrent"]
    assert relinks == [("skipped", True), ("applied", False)]
    assert lib.qbit.file_names(t.hash) == ["古诺希亚 S01E08.mkv"]
    assert out.stop == converge.FIXED_POINT

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


class _Upto3(_Chain):
    def detect(self, ctx, state):
        return [f for f in super().detect(ctx, state) if f.summary != "x3.mkv"]


def test_a_rename_that_stays_blocked_is_not_pending_at_the_cap(lib):
    """2026-09-27 审查：「集位被占」的跳过每次迭代都重试（`RETRYABLE`），所以到顶后的收尾诊断总把它列成待做——哪怕它在
    每一次迭代里都以同样的原因被挡、最后一次迭代也没有任何动作碰过占着的那个文件。真正收敛了的一轮于是报 `loop_cap`
    （"每轮都到顶，多半是有规则在拉锯"）；生产上长期挂着一个被占集位的库，每一轮用满迭代都会这样报。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    s1.local("x1.mkv")
    s1.local("a.mkv")
    s1.local("b.mkv")

    c = lib.loop(detectors=[_Upto3(), _Flip("blocked", "a.mkv", "b.mkv")], max_iterations=2)

    renames = [(r["status"], r["args"]["new_name"]) for r in lib.audit(c.run_id) if r["op"] == "rename"]
    assert renames.count(("skipped", "b.mkv")) == 2                     # 每次迭代都被挡
    assert c.outcome.stop == converge.FIXED_POINT and c.outcome.pending == []


def test_a_blocked_rename_whose_occupier_moved_away_is_still_pending(lib):
    """对照：最后一次迭代把占着目标名的文件改走了（`b.mkv → c.mkv`，排在被挡的改名之后）——下一次迭代它就改得成，
    仍然是待做。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show(SHOW).season(1)
    s1.local("x1.mkv")
    s1.local("a.mkv")
    s1.local("b.mkv")

    class Later:
        id = "later"

        def detect(self, ctx, state):
            for show in state.shows:
                for f in show.files:
                    if f.filename == "x2.mkv":                   # 第二次迭代才提：b.mkv → c.mkv
                        b = f.path.parent / "b.mkv"
                        yield Finding(rule=self.id, kind="later", severity="minor", summary="b → c",
                                      show=show.dir_name, path=str(b),
                                      action=Action(op="rename", args={
                                          "path": str(b), "new_name": "c.mkv", "torrent_hash": ""}))

    c = lib.loop(detectors=[_Upto3(), _Flip("blocked", "a.mkv", "b.mkv"), Later()], max_iterations=2)

    assert c.outcome.stop == converge.CAP
    assert [p.action.args["new_name"] for p in c.outcome.pending] == ["b.mkv"]


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


def test_undoes_knows_a_relink_reversed_and_a_regrab_after_the_record_was_lost(lib):
    """两个以前没测到的逆操作（2026-09-27 审查：把它们关掉的变异全套存活）：
    - relink 的反向——按执行器真记下的逆操作（映射反过来）认；原样再 relink 一次不是反向；
    - 隔离时连种子记录一起丢了（`torrent_record_lost`）：再把那个种子抓回来就是撤销那次隔离。"""
    from media_agent.plugins.builtin import StaleTorrentPathDetector
    size = 734_003_200
    s1 = lib.show("古诺希亚").season(1)
    t = s1.single("GNOSIA - S01E08 [WebRip 1080p HEVC-10bit AAC].mkv", size=size, on_disk=False)
    s1.local("古诺希亚 S01E08.mkv", size=size)
    [rec] = lib.cycle(detectors=[StaleTorrentPathDetector]).applied("relink_torrent")
    back = {"torrent_hash": t.hash, "mapping": rec["undo"]["mapping"]}
    assert converge.undoes("relink_torrent", back, rec)
    assert not converge.undoes("relink_torrent", rec["args"], rec)

    h = "a" * 40
    lost = {"status": "applied", "op": "trash", "args": {"torrent_hash": h},
            "undo": {"op": "restore_from_trash", "torrent_record_lost": True}}
    assert converge.undoes("grab_episode", {"url": f"https://mikanani.me/Download/20260901/{h}.torrent"}, lost)


@pytest.mark.parametrize("op", ["relocate", "rename_show_dir"])
def test_a_torrent_moved_this_run_is_not_dropped_as_dead_either(lib, op):
    """`TOUCHING` 里另外两个动作：setLocation 之后种子同样有一阵子"下载中、0 做种"。只有 relink 那一条有测试，
    把 relocate 从 `TOUCHING` 里拿掉、或不再解析目录改名的 `torrent_savepaths` 的变异全套存活（2026-09-27 审查）。
    这里用执行器真写下的审计记录（目录改名的逆操作形状就是它）喂给那道闸。"""
    from media_agent.actions import Executor
    from media_agent.plugins.builtin import TitleDriftDetector
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(77, "新名字", seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("旧名字")
    t = sh.season(1).single("新名字 S01E01.mkv")
    sh.sidecar(tmdb_id=77, tmdb_title="新名字")
    if op == "rename_show_dir":
        applied = lib.cycle(detectors=[TitleDriftDetector]).applied(op)
    else:
        mv = Finding(rule="move", kind="move", severity="minor", summary="挪存储", torrent_hash=t.hash,
                     action=Action(op="relocate", args={"torrent_hash": t.hash,
                                                        "location": str(lib.media_root / "别处")}))
        applied = lib.apply([mv]).applied
    [moved] = applied
    guard = converge._Guard(Executor(lib.context(), dry_run=True, run_id="t-guard"))
    guard.note([moved], 1)
    dead = Finding(rule="dead-torrent", kind="dead", severity="minor", summary="死种", torrent_hash=t.hash,
                   action=Action(op="drop_torrent", args={"torrent_hash": t.hash, "dead": True}))

    todo, memo, osc, deferred = guard.screen([dead], 2, write=False)

    assert (todo, deferred) == ([], 1)


def test_trashing_an_extra_of_a_just_grabbed_release_does_not_undo_the_grab():
    """只作废多文件发布里的一个条目（NCOP、PV）不撤销抓取；作废抓的那一集本身才撤销（2026-09-27 审查）。"""
    grab = {"status": "applied", "op": "grab_episode", "args": {},
            "undo": {"op": "ungrab_episode", "infohash": "abc123", "season": 1, "episode": 9}}
    ncop = {"path": "/m/尼古喵喵/Season 1/[LoliHouse] Yani Neko - NCOP [WebRip 1080p].mkv",
            "torrent_hash": "abc123", "file_only": True}
    assert not converge.undoes("trash", ncop, grab)
    own = {"path": "/m/尼古喵喵/Season 1/尼古喵喵 S01E09.mkv", "torrent_hash": "abc123", "file_only": True}
    assert converge.undoes("trash", own, grab)
    raw = {"path": "/m/尼古喵喵/Season 1/[LoliHouse] Yani Neko - 09 [WebRip 1080p].mkv",
           "torrent_hash": "abc123", "file_only": True}
    assert converge.undoes("trash", raw, grab)                           # 还没改名的那一集
    assert converge.undoes("trash", {**ncop, "file_only": False}, grab)   # 整个种子一起作废


def test_a_grabbed_release_with_an_extra_is_cleaned_without_a_false_oscillation(lib):
    """抓进来的发布里带着 NCOP：extras-in-library 在第二次迭代提议只作废那个条目——以前被当成"撤销本轮的抓取"拒绝、
    报 important 的 `oscillation`（"两条规则在打架"），下一轮又照常清掉。"""
    from harness import make_torrent
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=SHOW, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    blob, ih = make_torrent("[LoliHouse] Yani Neko - 09", files={
        "[LoliHouse] Yani Neko - 09 [WebRip 1080p].mkv": 600_000_000,
        "[LoliHouse] Yani Neko - NCOP [WebRip 1080p].mkv": 90_000_000})
    item = MikanItem(title=title, pub=dict(schedule)[9],
                     url=f"https://mikanani.me/Download/20260920/{ih}.torrent", torrent=blob, infohash=ih)
    lib.mikan("3500", [item], search=[SHOW])

    c = lib.loop()

    assert c.applied("grab_episode")
    assert not c.outcome.oscillations and "oscillation" not in c.kinds()
    trashes = [r for r in lib.audit(c.run_id) if r["op"] == "trash"]
    assert [(r["status"], "NCOP" in r["args"]["path"]) for r in trashes] == [("applied", True)]
    assert all(it.reversed == 0 for it in c.iterations)


def test_key_of_tells_episodes_of_the_same_season_apart():
    """抓第 9 集与抓第 10 集是两件事：`grab_episode` 按番目录 + 季 + **集**认。只按季认的话，第二次迭代里另一集的
    抓取会被当成"本轮已试过"（2026-09-27 审查：变异存活）。"""
    a = {"show_dir": "/m/尼古喵喵", "season": 1, "episode": 9, "url": "u9"}
    assert converge.key_of("grab_episode", a) != converge.key_of("grab_episode", {**a, "episode": 10})
    assert converge.key_of("grab_episode", a) == converge.key_of("grab_episode", {**a, "url": "另一个发布"})


def test_the_claims_index_is_rebuilt_for_every_iteration(lib):
    """占用索引按迭代作废（`Executor.new_iteration`）：两次迭代之间 qBittorrent 里的变化（别的动作摘掉的种子、AB 新加的）
    下一次迭代要看得见。第一次迭代建好索引之后再没有执行过动作时（索引不会因为写审计而作废），不清就一直用旧的
    （2026-09-27 审查：变异存活）。"""
    from media_agent.actions import Executor
    from media_agent.scan import build_state
    s1 = lib.show(SHOW).season(1)
    s1.single("a.mkv", category="旧分类")
    b = s1.single("b.mkv", progress=0.0)                          # 0%：盘上没有，但种子声明着这个路径
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")

    def scan(n):
        if n == 2:
            lib.qbit.delete([b.hash], False)                     # 两次迭代之间，占着的种子没了
        return build_state(ctx)

    reg = lib.registry([_Recat("recat", "旧分类", SHOW), _Flip("blocked", "a.mkv", "b.mkv")])
    converge.run(ctx, reg, ex, scan=scan, max_iterations=3)

    renames = [(r["status"], converge.retryable(r)) for r in lib.audit("t001") if r["op"] == "rename"]
    assert renames == [("skipped", True), ("applied", False)]


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


# ------------------------------------------------------------------ 以后的 `media-agent grab`（第 5 阶段）
def test_a_grab_only_pass_reuses_the_loop_with_only_grab_detectors(lib):
    """抓取模式还没有（第 5 阶段），但循环要能原样给它用：只有抓取检测器的 Registry + 只做抓取的 `select`，
    得到同样的一个执行器、不重试、到不动点就停。库里别的问题（待改名的发布名文件）一件都不碰。"""
    from media_agent.plugins.grab import GRAB_DETECTORS
    lib.configure(qbit_allow_empty=True)
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
    other = lib.show("别的番").season(1).single("[G] Other - 01 [1080p].mkv")   # 全量规则会改它的名

    c = lib.loop(detectors=GRAB_DETECTORS, select=converge.only("grab_episode"))

    assert [r["op"] for r in lib.audit(c.run_id)] == ["grab_episode"]
    assert lib.qbit.file_names(other.hash) == ["[G] Other - 01 [1080p].mkv"]
    assert c.outcome.stop == converge.FIXED_POINT and len(c.iterations) == 2   # 第二次：那一集已在下，不再抓
    assert lib.qbit.torrent(item.infohash)["tags"] == "ma:S01E09"


def test_only_selects_by_op():
    pick = converge.only("grab_episode", "rename")
    grab = Finding(rule="r", kind="k", severity="minor", summary="s",
                   action=Action(op="grab_episode", args={}))
    trash = Finding(rule="r", kind="k", severity="minor", summary="s", action=Action(op="trash", args={}))
    assert pick(grab) and not pick(trash)
    assert not pick(Finding(rule="r", kind="k", severity="minor", summary="s"))


# ------------------------------------------------------------------ 模型选的身份：这一轮不用
def test_an_identity_pinned_from_a_model_pick_is_not_acted_on_in_the_same_run(lib):
    """critic N4 的约定：模型在多个 TMDB 候选里选的条目**这一轮不用**——经 `pin_tmdb` 钉进 sidecar（有审计、能回退），
    下一轮起才按它改名 / 改目录名 / 改分类 / 抓取。迭代到不动点时，第二次迭代的扫描已经照 sidecar 认它了：不拦的话，
    模型的选择在钉进去的同一轮就改了目录名，人连看一眼 `tmdb_pick` 的机会都没有。"""
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(11, "葬送的芙莉莲", queries=["Frieren"], first_air_date="2023-09-29",
                      seasons={1: weekly(3, first_days_ago=900)})
    lib.tmdb.add_show(12, "葬送的芙莉莲 迷你剧场", queries=["Frieren"], first_air_date="2023-10-06")
    sh = lib.show("Frieren")
    sh.season(1).local("Frieren S01E01.mkv")
    lib.llm.when(lambda s, u: "Frieren" in u, {"id": 11, "confidence": 0.9, "reason": "正篇"})

    c = lib.loop()

    ops = [r["op"] for r in c.applied()]
    assert "pin_tmdb" in ops
    assert not {"rename", "rename_show_dir", "write_nfo", "recategorize"} & set(ops), ops
    assert sh.path.is_dir() and lib.disk() == {"Frieren/Season 1/Frieren S01E01.mkv": 600_000_000}
    [held] = [f for f in c.findings if f.kind == "naming_held"]
    assert "pin_tmdb" in held.summary

    nxt = lib.loop()                                            # 下一轮：照钉住的认
    assert "rename_show_dir" in [r["op"] for r in nxt.applied()]


@pytest.mark.parametrize("llm", [False, True], ids=["no-llm", "llm"])
def test_a_renamed_dir_keeps_the_identity_it_was_renamed_by(lib, llm):
    """2026-09-27 审查：没钉 tmdb_id 的番，第一次迭代按旧目录名搜到条目 77，按它写 NFO、把目录改成 TMDB 标题（op 8）；
    写 sidecar（op 10）因为目录已不在被跳过，身份没钉进去。第二次迭代按**新目录名**（就是 TMDB 标题）重新搜——同名的
    另一个条目（重制版、真人版）让它选了 88（没开模型取第一个、开了问模型再 `pin_tmdb`），此后只有人改得了。
    目录名是按 77 改的，身份就得跟着目录走：第二次迭代不再搜、不再问模型。"""
    from media_agent.actions import Executor
    from media_agent.scan import build_state
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(88, "同名", seasons={1: weekly(2, first_days_ago=9000)})
    lib.tmdb.add_show(77, "同名", queries=["旧名字"], seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("旧名字")
    for n in (1, 2):
        sh.season(1).local(f"同名 S01E{n:02d}.mkv")
    if llm:
        lib.llm.when(lambda s, u: "同名" in u, {"id": 88, "confidence": 0.8, "reason": "同名"})
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")
    net: dict[int, list] = {}

    def scan(n):
        mark = (len(lib.tmdb.calls), len(lib.llm.prompts))
        state = build_state(ctx)
        net[n] = lib.tmdb.calls[mark[0]:] + lib.llm.prompts[mark[1]:]
        return state

    out = converge.run(ctx, lib.registry(), ex, scan=scan, max_iterations=3)

    assert "rename_show_dir" in [r["op"] for r in ex.report.applied]
    assert out.stop == converge.FIXED_POINT and len(out.iterations) >= 2
    assert all(net[n] == [] for n in net if n > 1), net
    assert (lib.sidecar("同名").tmdb_id, lib.sidecar("同名").tmdb_source) == (77, "search")
    assert "77" in (lib.media_root / "同名" / "tvshow.nfo").read_text(encoding="utf-8")
    assert "pin_tmdb" not in [r["op"] for r in ex.report.applied]


# ------------------------------------------------------------------ qBittorrent 还在搬：不在半搬的视图上再诊断
def _moving_show(lib):
    """`旧名字/` 里两个有种子的文件 + 一个纯本地文件，sidecar 记着 1–3 集；TMDB 标题是 `新名字`——title-drift 改目录名。
    qBittorrent 5.2.3 的 setLocation 是异步的（`FakeQbit.async_moves`）：搬完之前种子是 `moving`、save_path 不变。"""
    lib.qbit.async_moves = True
    lib.tmdb.add_show(77, "新名字", seasons={1: weekly(3, first_days_ago=900)})
    sh = lib.show("旧名字")
    s1 = sh.season(1)
    t = s1.torrent({"新名字 S01E01.mkv": 600_000_000, "新名字 S01E02.mkv": 600_000_000},
                   name="新名字 01-02", layout="nosub")
    s1.local("新名字 S01E03.mkv")
    sh.sidecar(tmdb_id=77, tmdb_title="新名字", seasons={"1": {"have": [1, 2, 3]}})
    return sh, t


def test_the_next_iteration_waits_for_qbittorrent_to_finish_moving(lib, monkeypatch):
    """2026-09-27 审查：以前下一次迭代在 `apply` 返回的那一刻就扫描——种子还在旧目录里搬（moving），新目录里只有
    `_merge_tree` 挪过去的纯本地文件与档案。第二次迭代把两个目录当两部番：sidecar-sync 按半搬的视图把新目录的
    `have` 从 [1, 2, 3] 写成 [3]；第一次迭代给旧目录写的档案（目录还在，因为种子还在搬）留下一个只有档案的幽灵目录。
    现在先等 qBittorrent 搬完（有上限），再扫描。"""
    sh, t = _moving_show(lib)
    polls = []

    def sleep(_s):                                   # 等的这一会儿 qBittorrent 搬完了
        polls.append(_s)
        lib.qbit.drain()

    monkeypatch.setattr(converge, "_sleep", sleep)

    c = lib.loop()

    assert polls and c.outcome.stop == converge.FIXED_POINT
    new = lib.media_root / "新名字"
    assert lib.sidecar("新名字").seasons["1"]["have"] == [1, 2, 3]
    assert not [p for p in sh.path.rglob("*") if p.is_file()]      # 旧目录里没有幽灵档案
    assert sorted(p.name for p in (new / "Season 1").iterdir()) == [
        "新名字 S01E01.mkv", "新名字 S01E02.mkv", "新名字 S01E03.mkv"]


def test_a_move_that_does_not_finish_in_time_stops_the_loop(lib, monkeypatch):
    """等不到（跨卷搬运、qBittorrent 卡住）：不在半搬的视图上诊断，这一轮到此为止，剩下的下一轮做。第一次迭代给
    旧目录的写档案也不写（目录本轮已改名，档案跟着新目录走）。"""
    sh, t = _moving_show(lib)
    monkeypatch.setattr(converge, "SETTLE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(converge, "_sleep", lambda s: None)

    c = lib.loop()

    assert c.outcome.stop == converge.MOVING and len(c.iterations) == 1
    assert "还在搬" in c.outcome.unsettled
    assert lib.sidecar("新名字").seasons["1"]["have"] == [1, 2, 3]
    [ws] = [r for r in lib.audit(c.run_id) if r["op"] == "write_sidecar"]
    assert ws["status"] == "skipped" and "改名" in ws["reason"]
    lib.qbit.drain()
    assert not (sh.path / ".media-agent.json").exists()           # 没有只剩档案的幽灵目录


# ------------------------------------------------------------------ 刚动过的种子：这一轮不按死种摘（critic §3.3）
def test_a_torrent_relinked_this_run_is_not_dropped_as_dead_in_the_same_run(lib):
    """relink 之后 recheck：校验没全过（同样大小、内容不同的分片）的种子此刻是"下载中、0 做种、0 可用"——刚校验完，
    还没连上任何 peer。以前一轮只诊断一次，死种判定要等下一轮（6 小时后）；迭代时第二次迭代就会按死种把它摘掉
    （critic §3.3：relink → recheck → stalledDL → 死种，6 小时缩成几秒）。这一轮暂缓，下一轮再看。"""
    from media_agent.plugins.builtin import DeadTorrentDetector, StaleTorrentPathDetector
    size = 734_003_200
    s1 = lib.show("古诺希亚").season(1)
    t = s1.single("GNOSIA - S01E08 [WebRip 1080p HEVC-10bit AAC].mkv", size=size, on_disk=False,
                  num_complete=0, availability=0, added_hours_ago=2000)
    s1.local("古诺希亚 S01E08.mkv", size=size)
    real = lib.qbit._disk_progress
    lib.qbit._disk_progress = lambda tt: 0.5 if tt["hash"] == t.hash else real(tt)   # 分片只对上一半

    c = lib.loop(detectors=[StaleTorrentPathDetector, DeadTorrentDetector])

    assert c.applied("relink_torrent")
    assert lib.qbit.has(t.hash)                                  # 没摘
    [rec] = [r for r in lib.audit(c.run_id) if r["op"] == "drop_torrent"]
    assert rec["status"] == "skipped" and rec["reason"].startswith("刚动过的种子")
    assert rec["touched_by"]["op"] == "relink_torrent"
    assert [it.deferred for it in c.iterations] == [0, 1]

    nxt = lib.loop(detectors=[StaleTorrentPathDetector, DeadTorrentDetector])   # 下一轮：照常按死种处置
    assert nxt.applied("drop_torrent")


# ------------------------------------------------------------------ 整轮仍是一个回退单元
def test_title_decisions_from_every_iteration_are_kept(lib):
    """标题稳定闸一轮只记一次、按各次迭代的决定合起来记（`Outcome.title_decisions`）。只留最后一次迭代的，前面迭代里
    问过、后面迭代没再出现的条目（那部番的目录被改走、第二次扫描认不出它）就没记下来（2026-09-27 审查：变异存活）。"""
    from media_agent.actions import Executor
    from media_agent.scan import build_state
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(77, "某番", seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("某番")
    sh.season(1).local("某番 S01E01.mkv")
    sh.sidecar(tmdb_id=77, tmdb_title="某番", tmdb_source="human")
    lib.show(SHOW).season(1).local("x1.mkv")                      # 让这一轮迭代不止一次
    ctx = lib.context()

    def scan(n):
        state = build_state(ctx)
        if n > 1:
            state.title_decisions = {}                           # 后面的迭代这个条目没出现
        return state

    out = converge.run(ctx, lib.registry([_Upto3()]), Executor(ctx, dry_run=False, run_id="t001"),
                       scan=scan, max_iterations=3)

    assert len(out.iterations) >= 2 and 77 in out.title_decisions


def test_a_run_that_iterated_rolls_back_as_one_unit(lib):
    """一轮一个批次 ID：`rollback` 按审计序号倒着撤——先撤第二次迭代的改名与隔离，再撤第一次迭代的分类交接。"""
    s1, raw, sub = _ab_duplicate(lib)
    before = lib.disk()

    c = lib.loop()
    assert len(c.iterations) == 3
    res = lib.rollback(c.run_id)

    assert res["failed"] == 0 and res["skipped"] == 0
    assert res["torrent_records_lost"] == 1                    # 整种子作废的输家：记录回不来（与单次一样）
    assert lib.disk() == before
    assert lib.qbit.torrent(sub.hash)["category"] == "Bangumi"
    assert lib.qbit.file_names(sub.hash) == [LOLI_08]


# ------------------------------------------------------------------ 第二次迭代起不打网络（F2）
def test_iterations_after_the_first_diagnose_without_any_network_call(lib):
    """迭代的代价要可控：第二次迭代起的扫描 + 诊断一次网络都不打——TMDB 身份 / 标题 / 分集表、番组页、RSS、Mikan 搜索
    都走缓存，本地动作改变不了其中任何一样。所以每次迭代都跑全部检测器，不必按迭代挑（`converge` 模块文档）。"""
    from media_agent.actions import Executor
    from media_agent.scan import build_state
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 8):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    _ab_duplicate(lib)                                               # 第 8 集：让这一轮迭代三次
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=SHOW, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title="[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
                     pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=[SHOW])
    rss = "https://mikanani.me/RSS/Bangumi?bangumiId=3500&subgroupid=583"
    sh.bangumi(7, title_raw="Yani Neko", rss_link=rss, group_name="LoliHouse")
    lib.web.rss(rss, [f"[LoliHouse] Yani Neko - {n:02d} [WebRip 1080p]" for n in range(1, 10)])
    ctx = lib.context()
    ex = Executor(ctx, dry_run=False, run_id="t001")

    def calls():
        return len(lib.tmdb.calls) + len(lib.web.calls) + len(lib.llm.prompts)

    start: dict[int, int] = {}
    spent: dict[int, int] = {}

    def scan(n):
        start[n] = calls()
        return build_state(ctx)

    out = converge.run(ctx, lib.registry(), ex, scan=scan, max_iterations=3,
                       on_diagnose=lambda n, st, fs: spent.__setitem__(n, calls() - start[n]))

    assert out.stop == converge.FIXED_POINT and len(out.iterations) == 3
    assert "grab_episode" in [r["op"] for r in ex.report.applied]
    assert spent[1] > 0
    assert [spent[n] for n in (2, 3)] == [0, 0]


@pytest.mark.allow("log_failure", match="TMDB")
def test_a_tmdb_outage_costs_one_timeout_per_run_not_per_iteration(lib):
    """扫描自己的断路器（"这一轮 TMDB 出过一次错，其余的用缓存兜底"）以前按一次扫描算：TMDB 挂着时每次迭代都再等
    一次 20 秒超时。一个 Context 就是一轮 `run`，断路器跟着它走。"""
    import httpx
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(77, "某番", seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("某番")
    sh.season(1).local("某番 S01E01.mkv")
    sh.sidecar(tmdb_id=77, tmdb_title="某番")
    _ab_duplicate(lib)                                               # 让这一轮迭代不止一次
    calls = []

    def down(tv_id):
        calls.append(tv_id)
        raise httpx.ConnectTimeout("connect timed out")

    lib.tmdb.tv_detail = down

    c = lib.loop()

    assert len(c.iterations) >= 2
    assert calls == [77]


@pytest.mark.allow("log_failure", match="feed 失败")
def test_a_mikan_outage_costs_one_timeout_per_url_per_run(lib):
    """番组页 / RSS 拉不到：本地动作修不好别人的网站。以前每次诊断都重新请求——迭代到不动点时，Mikan 挂着的一轮要
    每次迭代把每个番组页各等一次 25 秒超时。同一个网址这一轮只请求一次，之后直接说"刚失败过"。"""
    import urllib.error
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 8):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    _ab_duplicate(lib)                                               # 让这一轮迭代不止一次
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=SHOW, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    feed = lib.mikan("3500", [], search=[SHOW]) or "https://mikanani.me/RSS/Bangumi?bangumiId=3500"

    def down(url):
        raise urllib.error.URLError("timed out")

    lib.web.route(feed, down)

    c = lib.loop()

    assert len(c.iterations) >= 2
    assert [u for u in lib.web.calls if "RSS/Bangumi" in u] == [feed]
    assert any("刚失败过" in m for m in lib.logs)

    lib.loop()                                                       # 下一轮（新进程）照常再试
    assert len([u for u in lib.web.calls if "RSS/Bangumi" in u]) == 2
