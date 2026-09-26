"""AutoBangumi 的 `episode_offset` 只换算**发布名里的原始集号**，不叠加到已规范的 `SxxEyy` 上。

生产 AB 订阅 id 37：《超超超超超喜欢你的100个女朋友》第三季按绝对集号 25–36 发布，
`episode_offset=-24` 把 `- 25` 换算成季内第 1 集，AB 据此改名成 `… S03E01.mkv`。
`builtin._resolve` 却对**每个**没钉 `ma:` 的文件都加这个偏移——已经是 `S03E01` 的规范名
再减 24，落进 `(3, -23)` 这个不存在的集位；刚下完、还叫 `- 25` 的新版本落进 `(3, 1)`。
于是同一集的两个文件永远不在同一个判重桶里（重复看不见），改名却撞上「集位被占」；
`kernel.episode_of_file`（`have` / 抓取 / 订阅健康的集号来源）又完全不看偏移，把 `- 25`
算成第 25 集——同一个文件在两条链路上是两个答案。生产 Season 3 目录里就是 `S03E01..`。
"""
from __future__ import annotations

from media_agent.kernel import episode_of_file, have_episodes
from media_agent.plugins.builtin import (DuplicateEpisodeDetector, UnrenamedDetector,
                                         _resolve)

TITLE = "超超超超超喜欢你的100个女朋友"
RAW_25 = "[Nekomoe kissaten] Hyakkano - 25 [1080p][JPSC].mkv"


def _season3(lib):
    sh = lib.show(TITLE)
    s3 = sh.season(3)
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    return sh, s3


def _files(lib):
    state = lib.scan(resolve_tmdb=False)
    [show] = state.shows
    return show, {f.filename: f for f in show.files}


def test_normalized_name_is_not_offset_again(lib):
    _, s3 = _season3(lib)
    s3.single(f"{TITLE} S03E01.mkv", name="[ANi] Hyakkano - 25 [1080P].mp4")
    s3.single(RAW_25)

    show, files = _files(lib)

    assert _resolve(files[f"{TITLE} S03E01.mkv"], show) == (3, 1)
    assert _resolve(files[RAW_25], show) == (3, 1)


def test_the_two_copies_of_one_episode_meet_in_one_bucket(lib):
    """以前 `(3, -23)` 与 `(3, 1)` 两个桶各一份：判重永远看不见这对重复。"""
    _, s3 = _season3(lib)
    s3.single(f"{TITLE} S03E01.mkv", name="[ANi] Hyakkano - 25 [1080P].mp4")
    s3.single(RAW_25)

    found = lib.diagnose(detectors=[DuplicateEpisodeDetector])

    dups = [f for f in found if f.kind == "duplicate"]
    assert len(dups) == 1
    assert "S03E01" in dups[0].summary


def test_kernel_episode_of_file_agrees_with_the_resolver(lib):
    _, s3 = _season3(lib)
    s3.single(f"{TITLE} S03E02.mkv", name="[ANi] Hyakkano - 26 [1080P].mp4")
    s3.single(RAW_25)

    show, files = _files(lib)

    assert episode_of_file(files[RAW_25], episode_offset=-24) == (3, 1)
    assert episode_of_file(files[f"{TITLE} S03E02.mkv"], episode_offset=-24) == (3, 2)
    assert have_episodes(show) == {3: {1, 2}}


def test_offset_that_would_give_a_non_positive_episode_is_unparsable(lib):
    """原始集号本来就是季内编号（`- 01`）时减 24 得到负数：以前会提议改成 `S03E-23`。"""
    _, s3 = _season3(lib)
    s3.single("[Other] Hyakkano S3 - 01 [1080p].mkv")

    show, files = _files(lib)
    [f] = files.values()
    assert _resolve(f, show) is None
    assert episode_of_file(f, episode_offset=-24) is None

    found = lib.diagnose(detectors=[UnrenamedDetector])
    assert [x.kind for x in found] == ["unparsable"]
    assert not [x for x in found if x.action]


def test_release_name_still_gets_the_offset(lib):
    """对照：发布名照旧换算（`- 25` → S03E01），这是偏移存在的理由。"""
    _, s3 = _season3(lib)
    t = s3.single(RAW_25)

    [f] = lib.diagnose(detectors=[UnrenamedDetector])

    assert f.action.args["new_name"] == f"{TITLE} S03E01.mkv"
    assert f.torrent_hash == t.hash
