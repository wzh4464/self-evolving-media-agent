"""封存冲突不再每轮撞一次「集位被占」。

两个不同的种子都钉着同一集、都复核通过（停滞放行换源、手动加了同钉子的种子），判重报 `seal_conflict`、
一个都不删，等人挑（删除关口 I4，`tests/test_duplicate_feeders.py`）。而 `unrenamed-file` 照样给两份都提
改名到同一个集位名：一份改成了，另一份此后**每一轮**都被执行器以「集位被占」跳过——生产上这类跳过连着
28 轮（runloop §8a），审计里一轮一条、没有任何结论。

现在：集位名归**已经叫这个名字**的那份，没有就归偏好分最高的那份；冲突里其余种子的文件不再提改名。
冲突只由判重报一次（`seal_conflict`，已归类、按集位认指纹），连着几轮都在就交给卡住检测。
"""
from __future__ import annotations

from harness import video
from media_agent import history
from media_agent.plugins.builtin import UnrenamedDetector, seal_conflicts

GB = 600_000_000
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")
LOLI = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
NEST = "[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv"
SLOT = "尼古喵喵 S01E08.mkv"


def _renames(findings):
    return {f.path: f.action.args["new_name"] for f in findings
            if f.action and f.action.op == "rename"}


def _occupied_skips(lib):
    return [r for r in lib.audit() if r["op"] == "rename" and r["status"] == "skipped"
            and "集位被占" in r.get("reason", "")]


def test_the_copy_already_holding_the_slot_name_keeps_it_the_other_is_left_alone(lib):
    s1 = lib.show("尼古喵喵").season(1)
    held = s1.single(SLOT, size=GB, name=LOLI, tags="ma:S01E08", probe=CHI)
    other = s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)

    found = lib.diagnose()

    assert _renames(found) == {}
    [conflict] = [f for f in found if f.kind == "seal_conflict"]
    assert conflict.subject == "S01E08" and conflict.classified and conflict.action is None
    assert conflict.evidence["slot_name_held_by"] == SLOT
    assert conflict.evidence["renames_held"] == [NEST]
    assert held.path.exists() and other.path.exists()


def test_both_release_named_only_the_preferred_one_gets_the_slot_name(lib):
    """以前两份都提改名：先排到的改成了，另一份此后每轮一条「集位被占」。"""
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.single(LOLI, size=GB, tags="ma:S01E08", probe=CHI)
    b = s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)
    [top] = [f for f in seal_conflicts(lib.scan().shows[0])[(1, 8)][:1]]

    rounds = [lib.cycle() for _ in range(4)]

    assert [len(c.applied("rename")) for c in rounds] == [1, 0, 0, 0]
    assert rounds[0].applied("rename")[0]["args"]["path"] == str(top.path)
    assert _occupied_skips(lib) == []
    assert all([f.kind for f in c.findings].count("seal_conflict") == 1 for c in rounds)
    # 集位级的指纹：第一轮后 path（桶里第一名）可能换了，身份不变
    fps = {history.fingerprint(f) for c in rounds for f in c.findings if f.kind == "seal_conflict"}
    assert len(fps) == 1
    assert lib.trash_files() == [] and lib.qbit.has(a.hash) and lib.qbit.has(b.hash)


def test_an_unsealed_third_copy_is_still_cleared_and_renames_stay_quiet(lib):
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(SLOT, size=GB, name=LOLI, tags="ma:S01E08", probe=CHI)
    s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)
    raw = s1.single("[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv",
                    size=GB + 9, probe=RAW)
    raw_ident = lib.ident(raw.path)

    lib.converge()

    assert [lib.ident(p) for p in lib.trash_files()] == [raw_ident]
    assert _occupied_skips(lib) == []


def test_subtitles_of_the_other_torrent_are_held_back_too(lib):
    """冲突的另一个种子里的外挂字幕同样落在这个集位的名字上（`尼古喵喵 S01E08.chs.ass`），一样会撞。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.torrent({SLOT: GB, "尼古喵喵 S01E08.chs.ass": 40_000}, name=LOLI, tags="ma:S01E08",
               probe={SLOT: CHI})
    s1.torrent({NEST: GB + 5, NEST.replace(".mkv", ".chs.ass"): 41_000}, name=NEST,
               tags="ma:S01E08", probe={NEST: CHI})

    assert _renames(lib.diagnose(detectors=[UnrenamedDetector])) == {}


def test_a_lone_pinned_copy_is_renamed_as_before(lib):
    """对照组：没有冲突（只有一个种子钉着这一集）时照常改名。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI, size=GB, tags="ma:S01E08", probe=CHI)

    assert _renames(lib.diagnose(detectors=[UnrenamedDetector])) == {str(t.path): SLOT}


def test_a_conflict_candidate_that_fails_its_check_is_no_conflict(lib):
    """另一份复核不过（探得到、确实没有中文字幕）：不是封存冲突，改名照旧交给判重之后的收敛。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(SLOT, size=GB, name=LOLI, tags="ma:S01E08", probe=CHI)
    s1.single("[Raw] Yani Neko - 08 [1080p].mkv", size=GB + 5, tags="ma:S01E08", probe=RAW)

    assert seal_conflicts(lib.scan().shows[0]) == {}


def test_rename_collision_does_not_report_the_same_conflict_again(lib):
    """`rename-collision`（critical）说的是 AutoBangumi 的改名死循环：两个种子的文件都要改成同一个名字。
    封存冲突的两份都是本项目抓的、在剧名分类下（AB 查不到），而且已经由判重报成 `seal_conflict`——
    再报一条 critical，同一个冲突每轮两条、卡住检测里也是两条。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(SLOT, size=GB, name=LOLI, tags="ma:S01E08", probe=CHI)
    s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)

    kinds = [f.kind for f in lib.diagnose()]

    assert kinds.count("seal_conflict") == 1 and "rename_collision" not in kinds


def test_rename_collision_still_reports_an_unsealed_competitor(lib):
    """对照组：第三份没钉 `ma:` 的也要这个名字——那是判重这一轮会清掉的普通重复，照报。"""
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(SLOT, size=GB, name=LOLI, tags="ma:S01E08", probe=CHI)
    s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)
    s1.single("[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv", size=GB + 9,
              probe=RAW)

    assert "rename_collision" in [f.kind for f in lib.diagnose()]
