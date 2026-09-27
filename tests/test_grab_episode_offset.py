"""抓取认集号偏移；归拢表里一集都没有时照样说出来（critic N13）。

**现场**（生产 AB 订阅 id 37）：《超超超超超喜欢你的100个女朋友》第三季的发布按连续集号编——`- 25` 是 S03E01，
AB 用 `episode_offset -24` 换算。抓取以前按发布的原始集号归拢候选（`by_ep[25]`），要找的是第 1 集：`by_ep[1]` 是空的，
一句 `continue`，既不抓也不报。AB 在的时候 AB 自己下；AB 一退役，这部番就无声地停了。

不变式：
- 集号偏移（`builtin.episode_offset_for`：sidecar 的 `episode_offsets`，没登记时是 AB 行在它那一季上的）换算原始集号，
  与判重 / 改名 / 出处账本同一个口径；换算出非正数的发布不收，报出来（为什么不收写在 `rejected_by_season` 里）。
- 要找的一集在归拢表里什么都没有：番组页上的编号落在这一季之外 → 报 `episode_numbering_mismatch`（多半是连续编号、
  没登记偏移）；否则播出超过 `NO_RELEASE_GRACE_DAYS` 天还没有任何发布 → 报 `episode_not_released`。刚播出的不报。
标题用生产上的名字形态，id、日期合成。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent import ledger
from media_agent.plugins.grab import NO_RELEASE_GRACE_DAYS, EpisodeAvailableDetector

TITLE = "超超超超超喜欢你的100个女朋友"
TMDB = 2203
MID = "3417"
NEKO = "[Nekomoe kissaten] Hyakkano - {n:02d} [1080p][简日内嵌]"
LOLI_S3 = "[LoliHouse] 超超超超超喜欢你的100个女朋友 第三季 / Hyakkano S3 - {n:02d} [WebRip 1080p][简繁内封字幕]"


def _grabs(findings) -> dict[str, object]:
    return {f.subject: f for f in findings if f.action and f.action.op == "grab_episode"}


def _hyakkano(lib, *, releases: dict[int, str], ab_offset=None, sidecar_offset=None,
              have=(3,), first_days_ago=40):
    """第三季周播 12 集，第 1 集 `first_days_ago` 天前播出；库里有 `have` 那几集。`releases`：季内集号 → 标题模板，
    模板里的 `{n}` 填发布方写的原始集号（`NEKO` 按连续编号：季内第 k 集写成 24 + k；`LOLI_S3` 按季内编号）。"""
    lib.configure(qbit_allow_empty=True)
    sched = weekly(12, first_days_ago=first_days_ago)
    lib.tmdb.add_show(TMDB, TITLE, seasons={3: sched})
    sh = lib.show(TITLE)
    for n in have:
        sh.season(3).local(f"{TITLE} S03E{n:02d}.mkv")
    fields = dict(tmdb_id=TMDB, tmdb_source="human", tmdb_title=TITLE, mikan_id=MID,
                  seasons={"3": {"have": list(have)}})
    if sidecar_offset is not None:
        fields["episode_offsets"] = {"3": sidecar_offset}
    sh.sidecar(**fields)
    if ab_offset is not None:
        sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=ab_offset)
    air = dict(sched)
    items = [MikanItem(title=tpl.format(n=(24 + k if tpl == NEKO else k)), pub=air[k])
             for k, tpl in releases.items()]
    lib.mikan(MID, items, search=[TITLE])
    return sh


def test_the_ab_offset_makes_continuous_numbers_grabbable(lib):
    """AB 37 的形态：`- 25` 是 S03E01。以前 `by_ep[25]`、找 `by_ep[1]`，什么都不抓也不报。"""
    _hyakkano(lib, ab_offset=-24, releases={k: NEKO for k in range(1, 7)})

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S03E01", "S03E02", "S03E04", "S03E05", "S03E06"}
    assert g["S03E01"].action.args["episode"] == 1
    assert "Hyakkano - 25" in g["S03E01"].action.args["title"]


def test_the_sidecar_offset_is_enough_once_ab_is_gone(lib):
    _hyakkano(lib, sidecar_offset=-24, releases={k: NEKO for k in range(1, 7)})

    g = _grabs(lib.diagnose(detectors=[EpisodeAvailableDetector]))

    assert set(g) == {"S03E01", "S03E02", "S03E04", "S03E05", "S03E06"}


def test_without_an_offset_the_numbering_mismatch_is_reported_not_skipped(lib):
    """没有偏移：番组页上的 25–30 落在 TMDB 第三季（12 集）之外。以前一个字都不说。"""
    _hyakkano(lib, releases={k: NEKO for k in range(1, 7)})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert not _grabs(fs)
    [f] = [f for f in fs if f.kind == "episode_numbering_mismatch"]
    assert f.subject == "S03" and f.severity == "important"
    assert f.evidence["unplaced"] == [25, 26, 27, 28, 29, 30]
    assert f.evidence["suggested_offset"] == -24
    assert "episode_offsets" in f.summary
    assert set(f.evidence["missing"]) == {1, 2, 4, 5, 6}


def test_a_registered_but_wrong_offset_gets_a_corrected_suggestion(lib):
    """登记了偏移、可登记错了（-12，发布其实从 25 起）：换算后 13–18 还是落在 12 集的一季之外，推测的偏移要把已登记的算进去
    ——是 -24，不是在 -12 之上再减的 -12（审查的变异 T1-15 忽略已登记的偏移，全套测试照样过）。"""
    _hyakkano(lib, sidecar_offset=-12, releases={k: NEKO for k in range(1, 7)})

    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.kind == "episode_numbering_mismatch"]

    assert f.evidence["episode_offset"] == -12 and f.evidence["unplaced"][0] == 13
    assert f.evidence["suggested_offset"] == -24


def test_a_release_the_offset_pushes_below_one_is_reported(lib):
    """配着 -24 的一季里按季内编号发的 `第三季 - 01`：换算出 -23，说不清是哪一集——不收，报出来、写明为什么。"""
    _hyakkano(lib, ab_offset=-24, releases={1: LOLI_S3, 2: NEKO, 4: NEKO, 5: NEKO, 6: NEKO})

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert set(_grabs(fs)) == {"S03E02", "S03E04", "S03E05", "S03E06"}
    [f] = [f for f in fs if f.subject == "S03E01"]
    assert not f.action and "明显属于别季" in f.summary
    assert any("换算出非正数" in r and "S3 - 01" in r for r in f.evidence["rejected_by_season"])


def test_an_episode_with_no_release_at_all_is_reported_after_a_grace(lib):
    """第 4、5 集播出一两周了，番组页上一个发布都没有（不是编号对不上）；第 6 集两天前才播，还在正常的等待里。"""
    _hyakkano(lib, sidecar_offset=-24, releases={1: NEKO, 2: NEKO}, first_days_ago=37)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert set(_grabs(fs)) == {"S03E01", "S03E02"}
    [f] = [f for f in fs if f.kind == "episode_not_released"]
    assert f.subject == "S03" and f.severity == "minor"
    assert f.evidence["episodes"] == [4, 5] and f.evidence["waiting"] == [6]
    assert f.evidence["mikan_id"] == MID and f.evidence["grace_days"] == NO_RELEASE_GRACE_DAYS
    assert not [f for f in fs if f.kind == "episode_numbering_mismatch"]


def test_the_grace_ends_on_the_day(lib):
    """播出正好 `NO_RELEASE_GRACE_DAYS` 天、还没有发布：宽限到头了，报（边界是"超过或等于"；审查的变异 T1-16 把 `<=`
    写成 `<`，全套测试照样过）。"""
    _hyakkano(lib, sidecar_offset=-24, releases={k: NEKO for k in range(1, 5)}, have=(1, 2, 3, 4),
              first_days_ago=7 * 4 + NO_RELEASE_GRACE_DAYS)

    [f] = [f for f in lib.diagnose(detectors=[EpisodeAvailableDetector]) if f.kind == "episode_not_released"]

    assert f.evidence["episodes"] == [5] and f.evidence["waiting"] == []


def test_a_just_aired_episode_without_a_release_is_quiet(lib):
    """只差两天前刚播的第 6 集：正常的等待，不报。"""
    _hyakkano(lib, sidecar_offset=-24, releases={k: NEKO for k in range(1, 6)},
              have=(1, 2, 3, 4, 5), first_days_ago=37)

    fs = lib.diagnose(detectors=[EpisodeAvailableDetector])

    assert not fs


def test_the_grab_pins_the_converted_slot(lib):
    """执行：钉子与出处账本记的是换算后的集位（S03E01），不是发布名里的 25。"""
    _hyakkano(lib, ab_offset=-24, releases={1: NEKO}, have=(2, 3, 4, 5, 6))

    c = lib.cycle(detectors=[EpisodeAvailableDetector])

    [rec] = c.applied("grab_episode")
    ih = rec["infohash"]
    assert "ma:S03E01" in (lib.qbit.torrent(ih).get("tags") or "")
    rows, problem = ledger.load_rows(lib.cfg.state_dir)
    assert not problem and rows[ih].slot == (3, 1)
