"""特典规则只看**作品标题之后**的那部分名字，而且不碰钉着 `ma:` 的正片、不碰某集唯一的文件。

`ExtrasDetector` 以前把 `is_extra` 用在整个文件名上，而规范名是 `{TMDB 标题} SxxEyy.ext`：
标题里带 `trailer` / `preview` / `menu` / `PV` / `特典` / `菜单` / `creditless` 的番，每一集都会
被当成特典移进隔离区——封存的也不例外。与"The Ghost **in** the Shell 整部被判成特典"同一类
（那次只修了 `IN` 这一个记号）。整改前核实：生产上 0 个标题命中，所以这是潜伏的。

删除关口对"特典处置、没钉 `ma:`"的文件放行（它们本来就该能删掉某集位唯一的文件），
挡不住这一类——所以要在检测器这里修。
"""
from __future__ import annotations

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
