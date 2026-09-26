"""特典规则只看**作品标题之后**的那部分名字，而且不碰钉着 `ma:` 的正片、不碰某集唯一的文件。

`ExtrasDetector` 以前把 `is_extra` 用在整个文件名上，而规范名是 `{TMDB 标题} SxxEyy.ext`：
标题里带 `trailer` / `preview` / `menu` / `PV` / `特典` / `菜单` / `creditless` 的番，每一集都会
被当成特典移进隔离区——封存的也不例外。与"The Ghost **in** the Shell 整部被判成特典"同一类
（那次只修了 `IN` 这一个记号）。整改前核实：生产上 0 个标题命中，所以这是潜伏的。

删除关口对"特典处置、没钉 `ma:`"的文件放行（它们本来就该能删掉某集位唯一的文件），
挡不住这一类——所以要在检测器这里修。
"""
from __future__ import annotations

import pytest

from harness import video
from media_agent.plugins.builtin import ExtrasDetector

GB = 600_000_000
CHI = video("hevc", subs=["chi 简体中文"])
TITLE = "异世界食堂的菜单"


def _menu_show(lib):
    s1 = lib.show(TITLE).season(1)
    eps = [s1.single(f"{TITLE} S01E{n:02d}.mkv", size=GB, name=f"[G] Isekai - {n:02d}.mkv")
           for n in (1, 2, 3)]
    return s1, eps


def test_marker_inside_the_canonical_title_is_not_an_extra(lib):
    _menu_show(lib)

    assert lib.diagnose(detectors=[ExtrasDetector]) == []


def test_whole_cycle_leaves_every_episode_in_place(lib):
    s1, eps = _menu_show(lib)
    before = lib.disk()

    lib.converge()

    assert lib.trash_files() == [] and lib.disk() == before
    assert all(lib.qbit.has(t.hash) for t in eps)


def test_marker_after_the_title_still_makes_an_extra(lib):
    """对照：标题之后的 NCOP 照旧是特典。"""
    s1, _ = _menu_show(lib)
    ncop = s1.single(f"{TITLE} NCOP.mkv", size=90_000_000)

    [f] = lib.diagnose(detectors=[ExtrasDetector])

    assert f.path == str(ncop.path) and f.action.op == "trash"


def test_release_name_whose_title_carries_a_marker_is_left_alone_as_the_only_copy(lib):
    """发布名里的罗马音标题带 `Trailer`，库里没有 AB 订阅可以认出标题：它是第 5 集唯一的
    文件，不当特典——宁可留着一个真特典，也不删掉唯一的一集。"""
    s1 = lib.show("拖车公园").season(1)
    s1.single("[G] Trailer Park Boys - 05 [1080p].mkv", size=GB)

    assert lib.diagnose(detectors=[ExtrasDetector]) == []


def _not_a_copy(s1, kind, filename):
    """同一集的"另一份"，其实放不了：幻影（种子说下完了、盘上没有——LAT-01、用户经 Jellyfin
    删文件）、0 字节的本地文件、比种子声明小的（截断）。"""
    from harness import write_sparse
    if kind == "phantom":
        s1.single(filename, size=GB, name="[X] Trailer Park Boys - 05.mkv", on_disk=False)
    elif kind == "zero-byte":
        s1.local(filename, size=0)
    else:
        t = s1.single(filename, size=GB, name="[X] Trailer Park Boys - 05.mkv")
        write_sparse(t.path, GB // 3, "truncated")


@pytest.mark.parametrize("kind", ["phantom", "zero-byte", "truncated"])
def test_a_copy_that_cannot_play_does_not_make_the_only_copy_an_extra(lib, kind):
    """2026-09-26 审查：`_only_copy` 以前数"同一集另一份下完了的正片"时不看它在不在盘上——一个幻影
    就让唯一真实的第 5 集被当特典隔离，关口对没钉子的特典又不问集位，run 30 天后把它硬删。"""
    s1 = lib.show("拖车公园").season(1)
    ep = s1.single("[G] Trailer Park Boys - 05 [1080p].mkv", size=GB)
    _not_a_copy(s1, kind, "拖车公园 S01E05.mkv")

    assert [f for f in lib.diagnose(detectors=[ExtrasDetector]) if f.path == str(ep.path)] == []


def test_the_backstop_holds_through_a_whole_cycle_with_a_phantom(lib):
    s1 = lib.show("拖车公园").season(1)
    ep = s1.single("[G] Trailer Park Boys - 05 [1080p].mkv", size=GB)
    _not_a_copy(s1, "phantom", "拖车公园 S01E05.mkv")
    ident = lib.ident(ep.path)

    lib.cycle()

    assert not [p for p in lib.trash_files() if lib.ident(p) == ident]
    assert any(lib.ident(p) == ident for p in s1.path.rglob("*.mkv"))


def test_pinned_episode_is_never_an_extra(lib):
    """钉着 `ma:S01E05` 的是抓取器认定的第 5 集——另有一份也一样，不归特典规则处置
    （它是判重的事，而且封存着）。"""
    s1 = lib.show("银八").season(1)
    t = s1.single("[G] Gintama Trailer Park - 05 [1080p].mkv", size=GB, tags="ma:S01E05",
                  probe=CHI)
    s1.local("银八 S01E05.mkv", size=GB)

    assert [f for f in lib.diagnose(detectors=[ExtrasDetector]) if f.path == str(t.path)] == []


def test_extra_inside_a_pinned_pack_is_still_an_extra(lib):
    """对照：钉子是整个种子的，合集里的 NCOP 不是那一集，照旧清理。"""
    s1 = lib.show("银八").season(1)
    t = s1.torrent({"银八 S01E05.mkv": GB, "NCOP1.mkv": 90_000_000}, name="[G] Gintama 05",
                   layout="original", tags="ma:S01E05", probe=CHI)
    ep, ncop = t.paths

    [f] = lib.diagnose(detectors=[ExtrasDetector])

    assert f.path == str(ncop)


# ------------------------------------------------------------------ 标题判据在每个检测器里都要起作用
# 2026-09-26 审查：把 `is_extra_of` 的剧名剥离关掉、去掉词边界、或让任一检测器回到整名匹配，全套测试
# 照样全绿（D09 / D10 / D14–D16b）——上面的现场每集只有一份，特典规则的"唯一一份"兜底把它们都掩护了。
def test_two_copies_of_an_episode_in_a_marker_titled_show_are_a_duplicate_not_extras(lib):
    """同一集两份：兜底不再掩护（另一份就在那），整名匹配的特典规则会把真正的一集当特典隔离，
    整名匹配的判重则看不见这对重复。"""
    s1, eps = _menu_show(lib)
    v2 = s1.local(f"{TITLE} S01E01 [v2].mkv", size=GB)

    c = lib.cycle()

    assert [r for r in c.applied("trash") if r["rule"] == "extras-in-library"] == []
    [dup] = [r for r in c.applied("trash") if r["rule"] == "duplicate-episode"]
    assert dup["args"]["path"] in (str(eps[0].path), str(v2))
    assert len([p for p in s1.path.glob(f"{TITLE} S01E01*.mkv")]) == 1
    assert all((s1.path / f"{TITLE} S01E{n:02d}.mkv").exists() for n in (2, 3))


def test_a_release_carrying_the_marked_title_is_still_renamed(lib):
    from media_agent.plugins.builtin import UnrenamedDetector

    s1 = lib.show(TITLE).season(1)
    t = s1.single(f"[G] {TITLE} - 04 [1080p].mkv", size=GB)

    [f] = lib.diagnose(detectors=[UnrenamedDetector])

    assert f.path == str(t.path) and f.action.args["new_name"] == f"{TITLE} S01E04.mkv"


def test_two_releases_of_a_marker_titled_show_racing_for_one_name_are_reported(lib):
    from media_agent.plugins.builtin import RenameCollisionDetector

    s1 = lib.show(TITLE).season(1)
    s1.single(f"[A] {TITLE} - 04 [1080p].mkv", size=GB)
    s1.single(f"[B] {TITLE} - 04 [720p].mkv", size=GB)

    [f] = lib.diagnose(detectors=[RenameCollisionDetector])

    assert f.evidence["target"] == f"{TITLE} S01E04.mkv"


@pytest.mark.parametrize("name,title", [
    ("NCOP.mkv", "OP"),                     # 标题 `OP` 不能把 NCOP 里的 OP 拿掉
    ("K [Tokuten][01].mkv", "K"),           # 标题 `K` 不能把 tokuten / mkv 里的 k 拿掉
])
def test_the_title_is_only_removed_on_word_boundaries(name, title):
    from media_agent.naming import is_extra_of

    assert is_extra_of(name, [title]) is True
