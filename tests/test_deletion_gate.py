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
    """特典处置是明确的"它不是正片"：名字碰巧认得出集号也可以删（没钉 `ma:`）。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12 [NCOP].mp4", size=GB)

    rep = lib.apply([_trash(t.path, t.hash, rule="extras-in-library", kind="extra_content",
                            show="朱音落语", file_only=True)])

    assert len(rep.applied) == 1 and not t.path.exists()


def test_i1_extras_disposition_of_a_pinned_only_copy_is_refused(lib):
    """钉了 `ma:` 的是抓取器认定的正片：特典规则误判了（标题里带 trailer / 菜单 …）也不能删。"""
    s1 = lib.show("朱音落语").season(1)
    t = s1.single("朱音落语 S01E12.mp4", size=GB, tags="ma:S01E12")

    rep = lib.apply([_trash(t.path, t.hash, rule="extras-in-library", kind="extra_content",
                            show="朱音落语", file_only=True)])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I1") and "ma:" in skip["reason"]
    assert t.path.exists()
