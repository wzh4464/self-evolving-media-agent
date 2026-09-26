"""删除关口（`media_agent/gate.py`）：每一次 trash / drop_torrent 生效之前，按**此刻**的
qBittorrent 与磁盘复核四条不变量，与产出它的是哪个检测器无关：

- I1 不让任何集位变成零个可播文件；
- I2 不删另一个保留着的种子仍然声明的路径；
- I3 不为了去掉一个文件整种子作废多文件种子（自动降级成只作废这一个条目）；
- I4 不删封存了集位的文件（`ma:` 钉子 + 复核通过；探测不可用 = 当作封存）。
- 另外：演进规则产出的删除一律不执行（第 1 阶段在 `_dispatch` 拦，这里是唯一的执法点）。

拒绝记 skipped、理由以「删除关口：」开头；看不全记 failed（与占用闸门同口径）。
"""
from __future__ import annotations

import pytest

from media_agent import gate
from media_agent.actions import Executor
from media_agent.kernel import DSL_ORIGIN, Action, Finding
from media_agent.plugins.builtin import ExtrasDetector

GB = 600_000_000


def _trash(path, h="", *, rule="duplicate-episode", kind="duplicate", show="尼古喵喵",
           evidence=None, **args):
    a = {"path": str(path), "torrent_hash": h, **args}
    return Finding(rule=rule, kind=kind, severity="important", summary=f"清理 {path}",
                   show=show, path=str(path), torrent_hash=h, evidence=evidence or {},
                   action=Action(op="trash", args=a))


def _priorities(lib, h):
    return {f["name"]: f["priority"] for f in lib.qbit.raw(h)["_files"]}


# ------------------------------------------------------------------ I2
def test_i2_path_another_kept_torrent_still_claims_is_not_trashed(lib):
    """两个种子声明同一个文件（生产 2026-09-06 尼古喵喵 S01E08 的形态）：要删的是单集种子 L
    的那一份，而合集 P 同样要这个文件。以前整种子摘掉 L、把文件搬走——P 从此缺一集，
    qBittorrent 会把它重新下回来或报 missingFiles。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"尼古喵喵 S01E05.mkv": GB, "尼古喵喵 S01E06.mkv": GB},
                      name="[G] Yani Neko 05-06", layout="nosub")
    loner = s1.single("尼古喵喵 S01E05.mkv", size=GB, name="[L] Yani Neko - 05.mkv",
                      on_disk=False)
    s1.local("尼古喵喵 S01E05 [other].mkv", size=GB)          # 集位里另有一份，I1 不挡
    before = lib.snapshot()

    rep = lib.apply([_trash(s1.path / "尼古喵喵 S01E05.mkv", loner.hash)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I2")
    assert pack.hash[:8] in skip["reason"]
    assert skip["deletion"]["gate"] == "I2"
    assert lib.snapshot() == before and lib.trash_files() == []


def test_i2_local_file_claimed_by_a_torrent_is_not_trashed(lib):
    """要删的在诊断里是纯本地文件（`torrent_hash` 为空），此刻却有种子声明着它。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB)
    s1.local("尼古喵喵 S01E07 [other].mkv", size=GB)
    before = lib.snapshot()

    rep = lib.apply([_trash(t.path, "")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I2") and t.hash[:8] in skip["reason"]
    assert lib.snapshot() == before


@pytest.mark.allow("failed_record", match="占用")
def test_i2_unknown_occupancy_is_a_failure_that_touches_nothing(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB)
    other = s1.single("尼古喵喵 S01E07 [other].mkv", size=GB)
    lib.qbit.fail("files", hash=other.hash, times=None)
    before = lib.snapshot()

    rep = lib.apply([_trash(t.path, t.hash)])

    [fail] = rep.failed
    assert "无法确认" in fail["error"] and "未做任何改动" in fail["error"]
    assert lib.snapshot() == before


# ------------------------------------------------------------------ I3
def test_i3_whole_torrent_request_on_a_pack_is_downgraded_to_this_entry(lib):
    """3年Z组银八老师 [01-12]（生产 12 个文件的合集）里的一集判重输了：以前整种子作废，
    其余 11 集失去做种，回退还加不回来（`torrent_record_lost`、没有 magnet）。
    生产审计 63 条 duplicate-episode 的整种子作废就是这么来的。"""
    s1 = lib.show("3年Z组银八老师").season(1)
    files = {f"3年Z组银八老师 S01E{n:02d}.mkv": GB for n in range(1, 13)}
    pack = s1.torrent(files, name="[G] 3-nen Z-gumi Ginpachi-sensei [01-12]", layout="nosub")
    s1.local("3年Z组银八老师 S01E05 [better].mkv", size=GB)
    e05 = s1.path / "3年Z组银八老师 S01E05.mkv"
    ident = lib.ident(e05)

    rep = lib.apply([_trash(e05, pack.hash, show="3年Z组银八老师")])

    [rec] = rep.applied
    assert lib.qbit.has(pack.hash)                                  # 合集还在做种
    pri = _priorities(lib, pack.hash)
    assert pri.pop("3年Z组银八老师 S01E05.mkv") == 0
    assert set(pri.values()) == {1}                                 # 其余 11 集照常
    assert rec["undo"]["torrent_record_lost"] is False
    assert any("I3" in n for n in rec["deletion"]["notes"])
    assert rec["deletion"]["subject"]["torrent_files"] == 12
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident


def test_i3_downgrade_matches_the_entry_by_full_relative_path(lib):
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"a/尼古喵喵 S01E05.mkv": GB, "b/尼古喵喵 S01E05.mkv": GB},
                      name="[G] Yani Neko 05 TV+Uncut", layout="nosub")
    target = s1.path / "b" / "尼古喵喵 S01E05.mkv"

    rep = lib.apply([_trash(target, pack.hash)])

    assert len(rep.applied) == 1
    assert _priorities(lib, pack.hash) == {"a/尼古喵喵 S01E05.mkv": 1,
                                           "b/尼古喵喵 S01E05.mkv": 0}
    assert (s1.path / "a" / "尼古喵喵 S01E05.mkv").exists() and not target.exists()


def test_i3_single_file_torrent_is_still_removed_whole(lib):
    """对照：种子只有这一个文件，整种子作废就是"只去掉这一个文件"。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB, name="[Z] Yani Neko - 07.mkv")
    s1.local("尼古喵喵 S01E07 [other].mkv", size=GB)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.applied
    assert not lib.qbit.has(t.hash) and rec["undo"]["torrent_record_lost"] is True
    assert "notes" not in rec["deletion"]


@pytest.mark.allow("failed_record", match="找不到")
def test_i3_file_that_is_not_an_entry_of_its_torrent_is_refused(lib):
    """要删的路径根本不是这个种子的条目：以前照样整种子摘掉、再搬一个不相干的文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB)
    stray = s1.local("尼古喵喵 S01E07 [stray].mkv", size=GB)
    before = lib.snapshot()

    rep = lib.apply([_trash(stray, t.hash)])

    assert len(rep.failed) == 1
    assert lib.snapshot() == before


def test_i3_phantom_member_of_a_pack_is_not_dropped_whole(lib):
    """幻影输家（种子声明了、盘上没有）若在合集里，同样只作废这一个条目，不摘整个合集。"""
    s1 = lib.show("朱音落语").season(1)
    pack = s1.torrent({"朱音落语 S01E11.mp4": GB, "朱音落语 S01E12.mp4": GB},
                      name="[G] Akane-banashi 11-12", layout="nosub")
    e12 = pack.paths[1]
    e12.unlink()

    rep = lib.apply([_trash(e12, pack.hash, show="朱音落语", phantom=True)])

    [rec] = rep.applied
    assert rec["priority_zeroed"] and lib.qbit.has(pack.hash)
    assert list(_priorities(lib, pack.hash).values()) == [1, 0]
    assert any("I3" in n for n in rec["deletion"]["notes"])


def test_i3_target_already_unwanted_leaves_its_torrent_alone(lib):
    """2026-09-26 审查（复现）：I3 按"要下载的条目数"定整种子作废还是只作废这一个，却从没问要删的这个
    是不是其中之一。它已是优先级 0、种子只剩**另一个**要下载的文件时，以前整种子摘掉——正在做种
    E06 的种子没了，回退也加不回来（`restore_from_trash` 没有 magnet）。它已经不下载了，种子不用动。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"尼古喵喵 S01E05.mkv": GB, "尼古喵喵 S01E06.mkv": GB},
                      name="[G] Yani Neko 05-06", layout="nosub",
                      priorities={"尼古喵喵 S01E05.mkv": 0})
    s1.local("尼古喵喵 S01E05 [BD].mkv", size=GB)
    e05 = s1.path / "尼古喵喵 S01E05.mkv"

    rep = lib.apply([_trash(e05, pack.hash, rule="manual", kind="manual")])

    [rec] = rep.applied
    assert lib.qbit.has(pack.hash)
    assert _priorities(lib, pack.hash) == {"尼古喵喵 S01E05.mkv": 0, "尼古喵喵 S01E06.mkv": 1}
    assert rec["undo"]["torrent_record_lost"] is False and not e05.exists()
    assert any("优先级 0" in n for n in rec["deletion"]["notes"])
    assert not [c for c in lib.qbit.calls if c[0] in ("delete", "set_file_priority")]


def test_i3_unwanted_member_of_an_original_layout_torrent_at_the_media_root(lib):
    """审查的整条链：Original 布局、save_path 就是媒体根的种子，scan 的来源 2 凭 content_path 把它的
    hash 挂到盘上的每个文件——包括优先级 0 的那个。判重输家正是它。"""
    from harness.library import DirBuilder

    sh = lib.show("尼古喵喵")
    t = DirBuilder(sh, lib.media_root).torrent(
        {"Season 1/尼古喵喵 S01E05.mkv": GB, "Season 1/尼古喵喵 S01E06.mkv": GB},
        name="尼古喵喵", layout="original", priorities={"Season 1/尼古喵喵 S01E05.mkv": 0},
        probe=RAW)
    sh.season(1).single("尼古喵喵 S01E05 [BD 1080p].mkv", size=4 * GB, probe=CHI,
                        name="[BD] Yani Neko - 05 [1080p].mkv")

    lib.cycle()

    assert lib.qbit.has(t.hash)
    assert (sh.path / "Season 1" / "尼古喵喵 S01E06.mkv").exists()


# ------------------------------------------------------------------ 演进规则
def test_evolved_rule_trash_is_refused_by_the_gate_itself(lib):
    """第 1 阶段在 `_dispatch` 拦演进规则；关口自己也认——绕过分派直接调 `_op_trash`
    （或以后有人改了分派）同样一个字节都不动。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB)
    s1.local("尼古喵喵 S01E07 [other].mkv", size=GB)
    f = _trash(t.path, t.hash, rule="llm-proposed", kind="llm_kind",
               evidence={"origin": DSL_ORIGIN, "source": "evolved"})
    ex = Executor(lib.context(), dry_run=False, run_id="ev")
    before = lib.snapshot()

    ex._op_trash(f, f.action)

    [skip] = ex.report.skipped
    assert skip["reason"].startswith("删除关口：") and "演进规则" in skip["reason"]
    assert lib.snapshot() == before


def test_screen_refuses_evolved_findings_for_every_destructive_op():
    for op in ("trash", "drop_torrent"):
        f = Finding(rule="x", kind="k", severity="minor", summary="",
                    evidence={"origin": DSL_ORIGIN},
                    action=Action(op=op, args={"path": "/x", "torrent_hash": "a" * 40}))
        v = gate.screen(f)
        assert v and v.startswith("删除关口：")
    ok = Finding(rule="duplicate-episode", kind="duplicate", severity="minor", summary="",
                 action=Action(op="trash", args={}))
    assert gate.screen(ok) == ""


# ------------------------------------------------------------------ 审计形状（给 purge 用）
def test_applied_trash_record_carries_the_deletion_facts(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E07.mkv", size=GB, name="[Z] Yani Neko - 07.mkv",
                  tags="ab:32", category="尼古喵喵")
    s1.local("尼古喵喵 S01E07 [other].mkv", size=GB)

    rep = lib.apply([_trash(t.path, t.hash)])

    [rec] = rep.applied
    d = rec["deletion"]
    assert d["gate"] == "passed" and d["disposition"] == "duplicate"
    assert d["slot"] == [1, 7]
    assert d["subject"] == {"torrent_hash": t.hash, "name": "[Z] Yani Neko - 07.mkv",
                            "pin": None, "tags": "ab:32", "category": "尼古喵喵",
                            "torrent_files": 1}
    assert rec["trashed_to"] and rec["undo"]["op"] == "restore_from_trash"   # 旧字段照旧


@pytest.mark.parametrize("rule,kind,expected", [
    ("duplicate-episode", "duplicate", "duplicate"),
    ("duplicate-episode", "bundled_version", "bundled_version"),
    ("extras-in-library", "extra_content", "extras"),
    ("dead-torrent", "dead_partial", "dead_partial"),
    ("manual", "manual", "manual"),
    ("something-else", "x", "other"),
])
def test_disposition_comes_from_the_producing_rule_not_from_args(rule, kind, expected):
    f = Finding(rule=rule, kind=kind, severity="minor", summary="",
                action=Action(op="trash", args={"disposition": "extras"}))
    assert gate.disposition_of(f) == expected


# ------------------------------------------------------------------ I1：点名了保留方
def _dup(loser, keeper, *, slot=(1, 8), keeper_hash=None, h=None, **kw):
    """判重输家的动作参数（D1 之后 duplicate-episode 就这么给）。"""
    k_path = keeper if not hasattr(keeper, "hash") else keeper.path
    k_hash = keeper_hash if keeper_hash is not None else getattr(keeper, "hash", "")
    l_path = loser if not hasattr(loser, "hash") else loser.path
    l_hash = h if h is not None else getattr(loser, "hash", "")
    return _trash(l_path, l_hash, keep_path=str(k_path), keep_hash=k_hash,
                  slot=list(slot), **kw)


def _pair(lib, **keeper_kw):
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    keeper = s1.single("尼古喵喵 S01E08.mkv", size=GB, name="[K] Yani Neko - 08.mkv",
                       **keeper_kw)
    return s1, loser, keeper


def test_i1_keeper_that_is_a_phantom_does_not_license_the_delete(lib):
    """保留方的种子说已下完、盘上却没有（LAT-01 之后留下的那种幻影）：以前输家照删，
    这一集从库里消失，30 天后隔离区到期就是永久丢失。"""
    s1, loser, keeper = _pair(lib, on_disk=False)
    before = lib.snapshot()

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "不在盘上" in skip["reason"]
    assert skip["deletion"]["keeper"]["path"] == str(keeper.path)
    assert lib.snapshot() == before


def test_i1_keeper_trashed_earlier_in_the_batch(lib):
    """同一批里保留方先被别的动作移进了隔离区，输家就不能再删。
    （执行顺序按 (动作, 剧, 路径) 排：保留方的路径排在输家前面。）"""
    s1 = lib.show("尼古喵喵").season(1)
    keeper = s1.single("[A] Yani Neko - 08 [1080p].mkv", size=GB)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    third = s1.local("尼古喵喵 S01E08 [third].mkv", size=GB)   # 让第一条删除本身合法
    first = _trash(keeper.path, keeper.hash, rule="manual", kind="manual")

    rep = lib.apply([first, _dup(loser, keeper)])

    assert [r["args"]["path"] for r in rep.applied] == [str(keeper.path)]
    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "本批次" in skip["reason"]
    assert loser.path.exists() and third.exists()


def test_i1_keeper_torrent_dropped_earlier_in_the_batch(lib):
    s1, loser, keeper = _pair(lib)
    ex = Executor(lib.context(), dry_run=False, run_id="u")
    ex._removed_torrents.add(keeper.hash)

    v = gate.check_trash(ex, _dup(loser, keeper), loser.path)

    assert v.gate == "I1" and "摘" in v.refused


def test_i1_keeper_still_downloading_is_not_a_keeper(lib):
    s1, loser, keeper = _pair(lib)
    raw = lib.qbit.raw(keeper.hash)                     # recheck 之后回到 99.8%
    raw["progress"] = 0.998
    raw["_files"][0]["progress"] = 0.998

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "没下完" in skip["reason"]


def test_i1_truncated_keeper_is_not_a_keeper(lib):
    """种子说下完了，盘上的文件却比声明的小：scan 的 `size` 是声明大小，排名看不出来。"""
    s1, loser, keeper = _pair(lib)
    with open(keeper.path, "r+b") as fp:
        fp.truncate(GB // 3)

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "截断" in skip["reason"]


def test_i1_local_keeper_smaller_than_at_diagnose_time(lib):
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    keeper = s1.local("尼古喵喵 S01E08.mkv", size=GB // 3)

    rep = lib.apply([_dup(loser, keeper, keeper_hash="", keep_size=GB)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "截断" in skip["reason"]


def test_i1_keeper_and_target_are_the_same_file(lib):
    """审计里 6 行「保留 X，清理 X」：两个种子声明同一路径，判重把唯一的真文件当输家。"""
    s1, loser, keeper = _pair(lib)

    rep = lib.apply([_dup(keeper, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "同一个文件" in skip["reason"]
    assert keeper.path.exists()


def test_i1_keeper_that_differs_from_the_target_only_in_case_is_the_same_file(lib, fs):
    """2026-09-26 审查（G17b）：生产卷是大小写不敏感的 APFS，`s01e05.MKV` 就是 `S01E05.mkv`。保留方
    路径与要删的只差大小写，按逐字比较会当成两个文件——删掉的正是保留方本身。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.local("尼古喵喵 S01E05.mkv", size=GB)
    ident = lib.ident(loser)

    rep = lib.apply([_dup(loser, s1.path / "尼古喵喵 s01e05.MKV", keeper_hash="",
                          slot=(1, 5), keep_size=GB)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "同一个文件" in skip["reason"]
    assert lib.ident(loser) == ident and lib.trash_files() == []


def test_i1_keeper_now_pinned_to_another_slot(lib):
    """诊断之后保留方的钉子变了（别的流程重打了 `ma:`）：它不再替这个集位作保。"""
    s1, loser, keeper = _pair(lib, tags="ma:S01E09")

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "S01E09" in skip["reason"]


def test_i1_target_no_longer_in_the_slot(lib):
    """要删的那个此刻钉着另一集：它不是这个集位的重复了。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB, tags="ma:S01E58")
    keeper = s1.single("尼古喵喵 S01E08.mkv", size=GB)

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "S01E58" in skip["reason"]


def test_i1_sound_keeper_lets_the_loser_go(lib):
    s1, loser, keeper = _pair(lib)
    ident = lib.ident(loser.path)

    rep = lib.apply([_dup(loser, keeper, keep_digest="abc")])

    [rec] = rep.applied
    assert rec["deletion"]["keeper"] == {"path": str(keeper.path), "hash": keeper.hash,
                                         "digest": "abc"}
    assert rec["deletion"]["slot"] == [1, 8]
    assert lib.ident(lib.trash_files()[0]) == ident


# ------------------------------------------------------------------ I1：没点名保留方
def test_i1_only_playable_file_of_a_slot_is_not_trashed(lib):
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=GB)
    before = lib.snapshot()

    rep = lib.apply([_trash(t.path, t.hash, rule="manual", kind="manual", show="朱音落语")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "S01E12" in skip["reason"]
    assert lib.snapshot() == before


@pytest.mark.parametrize("holder", ["partial", "dot_dir", "subtitle"])
def test_i1_things_that_are_not_a_playable_copy_do_not_count(lib, holder):
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=GB)
    if holder == "partial":
        s1.single("[B] Akane-banashi - 12.mp4", size=GB, progress=0.5)
    elif holder == "dot_dir":
        lib.show("朱音落语").folder("Season 1/.extras").local("朱音落语 S01E12.mp4", size=GB)
    else:
        s1.local("朱音落语 S01E12.ass", size=50_000)

    rep = lib.apply([_trash(t.path, t.hash, rule="manual", kind="manual", show="朱音落语")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1")


def test_i1_another_copy_trashed_earlier_in_the_batch_does_not_count(lib):
    s1 = lib.show("朱音落语").season(1)
    a = s1.single("朱音落语 S01E12.mp4", size=GB)
    b = s1.local("朱音落语 S01E12 [b].mp4", size=GB)

    rep = lib.apply([_trash(b, "", rule="manual", kind="manual", show="朱音落语"),
                     _trash(a.path, a.hash, rule="manual", kind="manual", show="朱音落语")])

    assert [r["args"]["path"] for r in rep.applied] == [str(b)]
    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1")
    assert a.path.exists()


def test_i1_extras_disposition_of_an_unpinned_file_is_allowed(lib):
    """特典处置是明确的"它不是正片"：名字认得出集号、而那一集另有真能播的正片时照删（没钉 `ma:`）。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12 [NCOP].mp4", size=GB)
    s1.local("朱音落语 S01E12.mp4", size=GB)

    rep = lib.apply([_trash(t.path, t.hash, rule="extras-in-library", kind="extra_content",
                            show="朱音落语", file_only=True)])

    assert len(rep.applied) == 1 and not t.path.exists()


def test_i1_extras_disposition_of_an_unpinned_only_copy_is_refused(lib):
    """2026-09-26 审查：没钉子的特典以前不问集位——名字认得出集号、那一集此刻又没有别的可播文件时，
    它可能就是那一集（发布名的罗马音标题里带 Trailer / Menu，检测器认不出标题）。检测器诊断时
    另一份还在、执行前被删了，或检测器的"唯一一份"兜底被幻影骗过（审查复现），关口都得拦下。"""
    s1 = lib.show("拖车公园").season(1)
    ep = s1.single("[G] Trailer Park Boys - 05 [1080p].mkv", size=GB)
    other = s1.local("拖车公园 S01E05.mkv", size=GB)
    findings = [f for f in lib.diagnose() if f.rule == "extras-in-library"]
    assert [f.path for f in findings] == [str(ep.path)]
    other.unlink()                                      # 诊断之后另一份没了

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "S01E05" in skip["reason"]
    assert ep.path.exists() and lib.trash_files() == []


def test_i1_extras_in_a_show_whose_title_carries_a_marker_count_its_episodes(lib):
    """同集的正片是规范名 `异世界食堂的菜单 S01E01.mkv`：标题里的「菜单」不能让它在关口那里也被当成
    特典、不算可播的那一份（与检测器同一口径：只看标题之后的部分）。"""
    title = "异世界食堂的菜单"
    s1 = lib.show(title).season(1)
    for n in (1, 2):
        s1.single(f"{title} S01E{n:02d}.mkv", size=GB, name=f"[G] Isekai - {n:02d}.mkv")
    nced = s1.single("[G] Isekai Shokudou NCED - 01 [1080p].mkv", size=90_000_000)

    c = lib.cycle()

    [rec] = [r for r in c.applied("trash") if r["rule"] == "extras-in-library"]
    assert rec["args"]["path"] == str(nced.path) and rec["deletion"]["slot"] == [1, 1]


def test_i1_extras_slot_comes_from_the_detector_not_the_raw_number(lib):
    """episode_offset -24 那一季的 NCOP 发布名写 `- 25`：检测器解析成 S03E01（库里有），关口若只凭
    名字猜成 S03E25（库里没有）就会每轮拒绝一次。动作里带上检测器算的集位。"""
    title = "超超超超超喜欢你的100个女朋友"
    sh = lib.show(title)
    s3 = sh.season(3)
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    s3.single(f"{title} S03E01.mkv", size=GB, name="[ANi] Hyakkano - 25 [1080P].mp4")
    ncop = s3.single("[ANi] Hyakkano NCOP - 25 [1080P].mkv", size=90_000_000)

    c = lib.cycle(detectors=[ExtrasDetector])

    [rec] = c.applied("trash")
    assert rec["args"]["path"] == str(ncop.path) and rec["deletion"]["slot"] == [3, 1]


def test_i1_extras_disposition_of_a_pinned_only_copy_is_refused(lib):
    """钉了 `ma:` 的是抓取器认定的正片：特典规则误判了（标题里带 trailer / 菜单 …）也不能删。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=GB, tags="ma:S01E12")

    rep = lib.apply([_trash(t.path, t.hash, rule="extras-in-library", kind="extra_content",
                            show="朱音落语", file_only=True)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "ma:" in skip["reason"]
    assert t.path.exists()


# ------------------------------------------------------------------ I4：封存
from harness import video  # noqa: E402

CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")                                   # 零字幕轨
LOLI = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"


def _sealed_pair(lib, *, loser_probe, loser_name=LOLI, loser_tags="ma:S01E08"):
    s1 = lib.show("尼古喵喵").season(1)
    keeper = s1.single("尼古喵喵 S01E08.mkv", size=GB, tags="ma:S01E08", probe=CHI,
                       name="[K] Yani Neko - 08 [简繁内封].mkv")
    loser = s1.single(loser_name, size=GB, tags=loser_tags, probe=loser_probe)
    return s1, keeper, loser


def test_i4_second_sealed_copy_from_another_torrent_is_not_trashed(lib):
    """两个不同的种子都钉着 S01E08、都复核通过（停滞 48 小时放行换源后常见）：
    以前只封存一个、另一个当输家删掉——哪个被封存取决于偏好分，两个都是择源的结论。"""
    s1, keeper, loser = _sealed_pair(lib, loser_probe=CHI)
    before = lib.snapshot()

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I4") and "封存" in skip["reason"]
    assert lib.snapshot() == before


def test_i4_probe_unavailable_keeps_the_seal(lib):
    """昨天探到两条中文字幕轨封存了，今天 ffprobe 超时（探测返回 None）：只看名字，LoliHouse
    的 `ASSx2` 过不了硬门槛——以前它就此失封、被当输家删掉。不知道 = 当作封存。"""
    s1, keeper, loser = _sealed_pair(lib, loser_probe=None)

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I4") and "探测不可用" in skip["reason"]
    assert loser.path.exists()


def test_i4_pinned_copy_that_verifiably_fails_review_is_not_sealed(lib):
    """探得到、而且确实没有中文字幕（ABEMA 生肉）：没封存，可以删。"""
    s1, keeper, loser = _sealed_pair(
        lib, loser_probe=RAW,
        loser_name="[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv")

    rep = lib.apply([_dup(loser, keeper)])

    assert len(rep.applied) == 1 and not loser.path.exists()


def test_i4_bundled_sibling_of_the_sealed_keeper_is_trashed(lib):
    """合并发布（TV 版 + 邪竜解放版同一个种子、同一个钉子）：封存由判重选中的那一份持有，
    兄弟按用户偏好只留一份（2026-09-18 尼古喵喵 EP11）。"""
    s1 = lib.show("尼古喵喵").season(1)
    bundle = s1.torrent({"【7月】尼古喵喵 11【TV版】.mp4": GB,
                         "【7月】尼古喵喵 11【邪龙解放版】.mp4": GB},
                        name="[TV版&无修版] 尼古喵喵 - EP11 [简／繁] (1080p H.264 AAC SRTx2)",
                        layout="nosub", tags="ma:S01E11", probe=CHI)
    tv, xie = bundle.paths
    f = _trash(tv, bundle.hash, kind="bundled_version", file_only=True,
               keep_path=str(xie), keep_hash=bundle.hash, slot=[1, 11])

    rep = lib.apply([f])

    [rec] = rep.applied
    assert rec["deletion"]["gate"] == "passed"
    assert _priorities(lib, bundle.hash) == {"【7月】尼古喵喵 11【TV版】.mp4": 0,
                                             "【7月】尼古喵喵 11【邪龙解放版】.mp4": 1}


def test_i4_extra_inside_a_pinned_torrent_is_not_a_seal(lib):
    """钉子是整个种子的：抓来的合集里的 NCOP 也带着 `ma:S01E05`，但它不是那一集。"""
    s1 = lib.show("银八").season(1)
    t = s1.torrent({"银八 S01E05.mkv": GB, "NCOP1.mkv": 90_000_000}, name="[G] Gintama 05",
                   layout="original", tags="ma:S01E05", probe=CHI)
    ep, ncop = t.paths

    rep = lib.apply([_trash(ncop, t.hash, rule="extras-in-library", kind="extra_content",
                            show="银八", file_only=True)])

    assert len(rep.applied) == 1 and not ncop.exists() and ep.exists()


def test_i4_pinned_episode_misread_as_an_extra_is_sealed(lib):
    """特典规则误判了一个钉着 `ma:` 的正片（它是种子里唯一的视频）：封存照样挡住。
    集位里另有一份，I1 不挡——挡住它的只有 I4。"""
    s1 = lib.show("银八").season(1)
    t = s1.single("[G] Gintama Trailer Park - 05 [1080p].mkv", size=GB, tags="ma:S01E05",
                  probe=CHI)
    s1.local("银八 S01E05.mkv", size=GB)

    rep = lib.apply([_trash(t.path, t.hash, rule="extras-in-library", kind="extra_content",
                            show="银八", file_only=True)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I4")
    assert t.path.exists()


# ------------------------------------------------------------------ drop_torrent
from media_agent.plugins.builtin import CollidingTorrentDetector  # noqa: E402

SLOT9 = "尼古喵喵 S01E09.mkv"


def _collision(lib):
    """穹庐下的魔女 E09 的形态：`- 09v2` 下完了，`- 09` 卡在 97.8%，两个种子指着同一个文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    keeper = s1.single(SLOT9, size=GB, name="[K] Yani Neko - 09v2.mkv")
    victim = s1.torrent({SLOT9: GB + 3}, name="[V] Yani Neko - 09.mkv", layout="single",
                        progress=0.978, state="stalledDL")
    return s1, keeper, victim


def test_drop_of_a_colliding_victim_still_works(lib):
    s1, keeper, victim = _collision(lib)

    c = lib.cycle(detectors=[CollidingTorrentDetector])

    [drop] = c.applied("drop_torrent")
    assert drop["deletion"]["gate"] == "passed"
    assert drop["deletion"]["keeper"]["hash"] == keeper.hash
    assert not lib.qbit.has(victim.hash) and lib.qbit.has(keeper.hash)


@pytest.mark.allow("failed_record", match="占用")
def test_drop_is_not_done_when_the_keepers_file_list_cannot_be_read(lib):
    """2026-09-26 审查：关口读不到保留方的文件列表（`check_drop` 记 failed）时执行器必须停手——
    没有测试注入过这个故障，忽略 `v.failed` 照样全绿（T08），受害者就在没确认保留方仍替那个
    共享文件作保的情况下被摘了。trash 那一侧的同一道（T02）有测试。"""
    s1, keeper, victim = _collision(lib)
    findings = lib.diagnose(detectors=[CollidingTorrentDetector])
    lib.qbit.fail("files", hash=keeper.hash, times=None)

    rep = lib.apply(findings)

    [fail] = [r for r in rep.failed if r["op"] == "drop_torrent"]
    assert fail["deletion"]["gate"] == "unknown" and "占用" in fail["error"]
    assert lib.qbit.has(victim.hash) and lib.qbit.has(keeper.hash)
    assert not [c for c in lib.qbit.calls if c[0] == "delete"]


def test_drop_is_refused_when_the_keeper_no_longer_claims_the_shared_file(lib):
    """诊断之后保留方改了名：共享的那个路径已经不归它了，受害者也不再是"撞车"——
    摘掉它就丢了一个也许能下完的下载，而那个路径谁也不保。"""
    s1, keeper, victim = _collision(lib)
    findings = lib.diagnose(detectors=[CollidingTorrentDetector])
    lib.qbit.rename_file(keeper.hash, SLOT9, "尼古喵喵 S01E09 [v2].mkv")

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "不再声明" in skip["reason"]
    assert lib.qbit.has(victim.hash)


def test_drop_is_refused_when_the_shared_file_is_gone_from_disk(lib):
    """保留方说下完了，盘上的文件却没了（幻影）：摘掉受害者，这一集就一份都没有了。"""
    s1, keeper, victim = _collision(lib)
    findings = lib.diagnose(detectors=[CollidingTorrentDetector])
    (s1.path / SLOT9).unlink()

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "不在盘上" in skip["reason"]
    assert lib.qbit.has(victim.hash)


def test_drop_is_refused_when_the_shared_file_is_truncated(lib):
    s1, keeper, victim = _collision(lib)
    findings = lib.diagnose(detectors=[CollidingTorrentDetector])
    with open(s1.path / SLOT9, "r+b") as fp:
        fp.truncate(GB // 2)

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "截断" in skip["reason"]


def test_evolved_drop_is_refused_by_the_gate_itself(lib):
    s1, keeper, victim = _collision(lib)
    [f] = lib.diagnose(detectors=[CollidingTorrentDetector])
    f.evidence["origin"] = DSL_ORIGIN
    ex = Executor(lib.context(), dry_run=False, run_id="ev")

    ex._op_drop_torrent(f, f.action)

    [skip] = ex.report.skipped
    assert skip["reason"].startswith("删除关口：") and "演进规则" in skip["reason"]
    assert lib.qbit.has(victim.hash)


# ------------------------------------------------------------------ 端到端：检测器 → 关口 → 执行器
def test_e2e_i2_scan_sees_one_claimer_but_the_gate_sees_both(lib):
    """两个种子声明同一个文件（合集 P 与单集 L，声明大小相同）：scan 每个路径只出一条，判重只看见
    其中一个种子。更好的版本 Y 到了，X 判输——以前整种子摘掉看得见的那个、把文件搬走，另一个
    种子从此缺一集（2026-09-06 尼古喵喵 S01E08 的双声明形态）。"""
    s1 = lib.show("尼古喵喵").season(1)
    x = "尼古喵喵 S01E05.mkv"
    pack = s1.torrent({x: GB, "尼古喵喵 S01E06.mkv": GB}, name="[P] Yani Neko 05-06",
                      layout="nosub", probe=RAW)
    loner = s1.single(x, size=GB, name="[L] Yani Neko - 05.mkv", on_disk=False)
    s1.single("[LoliHouse] Yani Neko - 05 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv",
              size=GB - 3, probe=CHI)
    ident = lib.ident(s1.path / x)

    c = lib.cycle()

    [skip] = c.skipped("trash")
    assert skip["reason"].startswith("删除关口：I2")
    assert lib.ident(s1.path / x) == ident and lib.trash_files() == []
    assert lib.qbit.has(pack.hash) and lib.qbit.has(loner.hash)
    assert not c.failed()


def test_e2e_i1_keeper_deleted_between_diagnose_and_apply(lib):
    """诊断之后、执行之前保留方没了（用户经 Jellyfin 删了它；或者被挪走）：输家不删。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv",
                      size=GB + 9, probe=RAW)
    keeper = s1.single("[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv",
                       size=GB, probe=CHI)
    ctx = lib.context()
    findings = [f for f in lib.diagnose(ctx=ctx) if f.action and f.action.op == "trash"]
    assert [f.action.args["path"] for f in findings] == [str(loser.path)]
    keeper.path.unlink()

    rep = lib.apply(findings, ctx=ctx)

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "不在盘上" in skip["reason"]
    assert loser.path.exists() and lib.qbit.has(loser.hash)


# ------------------------------------------------------------------ 每道闸单独的现场
# 变异验证（逐个关掉关口里的判断）时，下面这些判断被相邻的判断掩护着——关掉它，别的判断照样
# 拦住原来的现场。每条只让一道闸有机会拦住它。

@pytest.mark.allow("failed_record", match="占用")
def test_i2_unknown_is_a_failure_even_when_i1_would_not_look(lib):
    """字幕不是可播文件，I1 不看别的种子；看不全只能由 I2 自己报。"""
    s1 = lib.show("尼古喵喵").season(1)
    sub = s1.local("尼古喵喵 S01E07.ass", size=50_000)
    other = s1.single("尼古喵喵 S01E07.mkv", size=GB)
    lib.qbit.fail("files", hash=other.hash, times=None)

    rep = lib.apply([_trash(sub, "", rule="manual", kind="manual")])

    [fail] = rep.failed
    assert "路径的占用情况" in fail["error"]
    assert sub.exists()


def test_i1_keeper_path_already_trashed_in_the_batch_is_refused_by_name(lib):
    s1, loser, keeper = _pair(lib)
    ex = Executor(lib.context(), dry_run=False, run_id="u")
    ex._trashed_paths.add(str(keeper.path))

    v = gate.check_trash(ex, _dup(loser, keeper), loser.path)

    assert v.gate == "I1" and "隔离区" in v.refused


def test_i1_partial_keeper_is_not_a_keeper(lib):
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    part = s1.local("尼古喵喵 S01E08.mkv.!qB", size=GB)

    rep = lib.apply([_dup(loser, part, keeper_hash="", keep_size=GB)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "半成品" in skip["reason"]


def test_i1_keeper_torrent_gone_from_qbit(lib):
    s1, loser, keeper = _pair(lib)
    f = _dup(loser, keeper)
    lib.qbit.delete([keeper.hash], delete_files=False)       # 诊断之后被人删了记录，文件还在

    rep = lib.apply([f])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "已不在 qBittorrent" in skip["reason"]


def test_i1_keeper_torrent_incomplete_though_its_file_is_done(lib):
    """保留方在一个还没下完的合集里（它这一集下完了）：种子进度不是 1，不作保——判重本来就
    只收下完的种子，执行时对不上说明状态变了。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    pack = s1.torrent({"尼古喵喵 S01E08.mkv": GB, "尼古喵喵 S01E09.mkv": GB},
                      name="[P] Yani Neko 08-09", layout="nosub")
    lib.qbit.raw(pack.hash)["progress"] = 0.5
    lib.qbit.raw(pack.hash)["_files"][1]["progress"] = 0.0
    keep = s1.path / "尼古喵喵 S01E08.mkv"

    rep = lib.apply([_dup(loser, keep, keeper_hash=pack.hash)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "还没下完" in skip["reason"]


def test_i1_keeper_entry_not_done_is_not_a_keeper(lib):
    s1, loser, keeper = _pair(lib)
    lib.qbit.raw(keeper.hash)["_files"][0]["progress"] = 0.5

    rep = lib.apply([_dup(loser, keeper)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "还没下完" in skip["reason"]


def test_i1_keeper_entry_set_to_skip_is_not_a_keeper(lib):
    """保留方的条目被设为不下载（文件还在盘上，但种子不再要它）：不再替这一集作保。"""
    s1 = lib.show("尼古喵喵").season(1)
    loser = s1.single("[Z] Yani Neko - 08 [1080p].mkv", size=GB)
    pack = s1.torrent({"尼古喵喵 S01E08.mkv": GB, "尼古喵喵 S01E09.mkv": GB},
                      name="[P] Yani Neko 08-09", layout="nosub",
                      priorities={"尼古喵喵 S01E08.mkv": 0})

    rep = lib.apply([_dup(loser, s1.path / "尼古喵喵 S01E08.mkv", keeper_hash=pack.hash)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "不再声明" in skip["reason"]


def test_i1_holder_already_trashed_in_the_batch_does_not_count_by_name(lib):
    s1 = lib.show("朱音落语").season(1)
    a = s1.single("朱音落语 S01E12.mp4", size=GB)
    b = s1.local("朱音落语 S01E12 [b].mp4", size=GB)
    ex = Executor(lib.context(), dry_run=False, run_id="u")
    ex._trashed_paths.add(str(b))

    v = gate.check_trash(ex, _trash(a.path, a.hash, rule="manual", kind="manual",
                                    show="朱音落语"), a.path)

    assert v.gate == "I1"


def test_i1_holder_whose_torrent_has_not_finished_it_does_not_count(lib):
    """另一份的文件已经在盘上（名字是正名），但它的种子说这个条目还没下完（recheck 中）。"""
    s1 = lib.show("朱音落语").season(1)
    a = s1.single("朱音落语 S01E12.mp4", size=GB)
    b = s1.single("朱音落语 S01E12 [b].mp4", size=GB)
    lib.qbit.raw(b.hash)["_files"][0]["progress"] = 0.9

    rep = lib.apply([_trash(a.path, a.hash, rule="manual", kind="manual", show="朱音落语")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1")


def test_drop_keeper_removed_earlier_in_the_batch(lib):
    s1, keeper, victim = _collision(lib)
    [f] = lib.diagnose(detectors=[CollidingTorrentDetector])
    ex = Executor(lib.context(), dry_run=False, run_id="u")
    ex._removed_torrents.add(keeper.hash)

    v = gate.check_drop(ex, f, lib.qbit.torrent(victim.hash))

    assert v.gate == "I1" and "本批次" in v.refused


# 2026-09-26 审查：没点名保留方时 I1 全靠 `other_holders` 只数"真能播的另一份"。下面四道过滤各自
# 关掉、全套测试照样全绿（G37 / G36 / G33 / G38）——而关掉任何一道，某集唯一完整的那一份就会被
# 一个"不是拷贝的东西"作保、删掉。
@pytest.mark.parametrize("other,size", [
    ("尼古喵喵 S01E05 [v2].mkv.!qB", GB // 3),           # 盘上的孤儿半成品（没有种子声明）
    ("尼古喵喵 S01E05 [v0].mkv", 0),                     # 0 字节的本地文件
    ("尼古喵喵 S01E05 NCED.mkv", GB // 3),               # 名字认得出这一集的特典
], ids=["orphan-partial", "zero-byte", "extra"])
def test_i1_the_only_complete_copy_is_not_vouched_for_by_a_non_copy(lib, other, size):
    s1 = lib.show("尼古喵喵").season(1)
    only = s1.local("尼古喵喵 S01E05.mkv", size=GB)
    s1.local(other, size=size)

    rep = lib.apply([_trash(only, "", rule="manual", kind="manual")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "S01E05" in skip["reason"]
    assert only.exists() and lib.trash_files() == []


def test_i1_a_file_pinned_to_another_episode_does_not_vouch_by_its_name(lib):
    """名字叫 S01E08、种子钉着 `ma:S01E58`（2026-08-31 Re:Zero 的形态）：钉子是抓取器的定论，它是
    第 58 集，不替第 8 集作保。"""
    s1 = lib.show("尼古喵喵").season(1)
    only = s1.local("尼古喵喵 S01E08.mkv", size=GB)
    s1.single("尼古喵喵 S01E08 [Fyy].mkv", size=GB, tags="ma:S01E58",
              name="[Fyy Raws] Yani Neko 3rd Season - 08 [1080p].mkv")

    rep = lib.apply([_trash(only, "", rule="manual", kind="manual")])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1")
    assert only.exists()


def test_i1_a_real_other_copy_does_vouch(lib):
    """对照：同一个现场，另一份是完整的本地文件——照删。"""
    s1 = lib.show("尼古喵喵").season(1)
    only = s1.local("尼古喵喵 S01E05.mkv", size=GB)
    s1.local("尼古喵喵 S01E05 [v2].mkv", size=GB)

    rep = lib.apply([_trash(only, "", rule="manual", kind="manual")])

    assert len(rep.applied) == 1 and not only.exists()


# 2026-09-26 审查：关口记下的集位"动作给的 slot 优先"关掉（G46）、隔离之前同批整种子作废时记下被摘的
# 是谁（T05），全套照样全绿——审计里的这两样是 purge 判"证明替代者""复排"的依据。
def test_the_recorded_slot_is_the_detectors_not_the_raw_number(lib):
    """episode_offset -24 那一季：判重按 S03E01 分桶，输家的发布名写的是 `- 25`。审计里的
    `deletion.slot` 必须是 S03E01——purge 按它去找替代者。"""
    title = "超超超超超喜欢你的100个女朋友"
    sh = lib.show(title)
    s3 = sh.season(3)
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    s3.single(f"{title} S03E01.mkv", size=GB, tags="ma:S03E01", probe=CHI,
              name="[ANi] Hyakkano - 25 [1080P].mkv")
    loser = s3.single("[Nekomoe kissaten] Hyakkano - 25 [720p][JPSC].mkv", size=GB // 2,
                      probe=RAW)
    from media_agent.plugins.builtin import DuplicateEpisodeDetector

    c = lib.cycle(detectors=[DuplicateEpisodeDetector])

    [rec] = c.applied("trash")
    assert rec["args"]["path"] == str(loser.path)
    assert rec["deletion"]["slot"] == [3, 1]


def test_a_later_trash_from_a_torrent_dropped_earlier_in_the_batch_remembers_it(lib):
    """同一批里先整种子作废了合集（要下载的只剩它自己那一集），后面又隔离它一个不下载的特典：
    那时种子已经问不到了，`deletion.subject` 用摘除那一刻记下的。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"尼古喵喵 S01E05.mkv": GB, "尼古喵喵 SP01.mkv": 90_000_000},
                      name="[G] Yani Neko 05+SP", layout="nosub",
                      priorities={"尼古喵喵 SP01.mkv": 0}, tags="ab:7")
    s1.local("尼古喵喵 S01E05 [BD].mkv", size=GB)
    e05, sp = s1.path / "尼古喵喵 S01E05.mkv", s1.path / "尼古喵喵 SP01.mkv"

    rep = lib.apply([_trash(e05, pack.hash, rule="manual", kind="manual"),
                     _trash(sp, pack.hash, rule="extras-in-library", kind="extra_content",
                            file_only=True)])

    first, second = rep.applied
    assert first["args"]["path"] == str(e05) and first["undo"]["torrent_record_lost"] is True
    assert second["args"]["path"] == str(sp)
    sub = second["deletion"]["subject"]
    assert sub["removed_this_batch"] is True and sub["name"] == "[G] Yani Neko 05+SP"
    assert sub["tags"] == "ab:7"
