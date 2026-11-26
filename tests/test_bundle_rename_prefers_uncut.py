"""合并发布（一个种子里 TV 版 + 无删减版）改名时，集位名必须给偏好分高的那份。

2026-11-18《学生会也有洞！》S01E07：抓取按 `+150 +无删减/简中` 选中了
`[TV版&无修版] 学生会也有洞！ - EP07 [简／繁]`，种子里两个文件都钉着 `ma:S01E07`：

    【10月】学生会也有洞！ 07【TV放送版】.mp4      495MB   ← 种子里排第一
    【10月】学生会也有洞！ 07【限制解除版】.mp4    648MB

`unrenamed-file` 给两份都提了改名到 `学生会也有洞！ S01E07.mp4`，执行器按文件顺序先改了 TV 版，
限制解除版被「集位被占」跳过——用户要的那份反而没拿到集位名。跨种子的封存冲突早有 `slot_holder`
（`tests/test_seal_conflict_quiet.py`），同一个种子里的撞车没人管。

现在：同一个种子里钉着同一集的视频不止一份时，谁都不改名，先让判重用原文件名挑（`bundled_version` 只作废落选
那一个文件），这一集只剩一份之后改名在同一轮的收敛循环里接上。

2026-11-22 S01E08 又错了一次（v0.5.3 已在生产）：数「有几份」时把盘上没有的文件当幻影排除了，而抓取后的头几秒
两个文件都还没落盘——保护在它唯一要管的那一刻没生效，抓取 3 秒后 TV 版又先拿到了集位名。上面那几个用例只搭了
「已下 30%」（盘上有 `.!qB`）和「已下完」，没搭「刚加进来」。下半部分的用例走真实的抓取动作把那一刻搭出来。
"""
from __future__ import annotations

import os

import pytest

from harness import video
from media_agent import converge
from media_agent.kernel import Action, Finding

CHI = video("h264", subs=["chi 简体中文", "chi 繁體中文"])
TV = "【10月】学生会也有洞！ 07【TV放送版】.mp4"
UNCUT = "【10月】学生会也有洞！ 07【限制解除版】.mp4"
SLOT = "学生会也有洞！ S01E07.mp4"
BUNDLE = "[TV版&无修版] 学生会也有洞！ - EP07 [简／繁] (1080p H.264 AAC SRTx2)"


def _renames(findings):
    return {f.path.rsplit("/", 1)[-1]: f.action.args["new_name"] for f in findings
            if f.action and f.action.op == "rename"}


def _bundle(lib, tv_first=True, **kw):
    s1 = lib.show("学生会也有洞！").season(1)
    files = {TV: 495_339_739, UNCUT: 648_826_375}
    if not tv_first:
        files = dict(reversed(list(files.items())))
    t = s1.torrent(files, name=BUNDLE, layout="nosub", tags="ab:42, ma:S01E07", probe=CHI, **kw)
    return s1, t


@pytest.mark.parametrize("tv_first", [True, False])
def test_neither_file_is_renamed_before_the_dedupe_has_picked(lib, tv_first):
    """两份都还叫原名时谁都不改：改了名「限制解除版」这个判据就没了。种子里的顺序不影响。"""
    _bundle(lib, tv_first=tv_first)

    found = lib.diagnose()

    assert _renames(found) == {}
    [void] = [f for f in found if f.kind == "bundled_version"]
    assert void.path.endswith(TV)


def test_while_downloading_nothing_is_renamed(lib):
    """生产上改名发生在抓取后两秒、文件还在下。判重不看下载中的种子，这时先改名就只能凭顺序。"""
    _bundle(lib, progress=0.3)

    assert _renames(lib.diagnose()) == {}


def test_once_the_download_finishes_the_slot_name_goes_to_the_uncut_file(lib):
    s1, t = _bundle(lib, progress=0.3)
    lib.cycle()
    t.complete()
    uncut_ident = lib.ident(s1.path / UNCUT)

    lib.converge()                          # 生产的 run：判重作废 TV 版，下一次迭代给限制解除版改名

    assert lib.ident(s1.path / SLOT) == uncut_ident
    assert lib.disk() == {f"学生会也有洞！/Season 1/{SLOT}": 648_826_375}


def test_after_a_round_the_slot_name_is_the_uncut_file_and_nothing_is_skipped(lib):
    s1, t = _bundle(lib)
    uncut_ident = lib.ident(s1.path / UNCUT)

    for _ in range(3):
        lib.cycle()

    assert lib.ident(s1.path / SLOT) == uncut_ident
    assert not [r for r in lib.audit() if r["op"] == "rename" and r["status"] == "skipped"
                and "集位被占" in r.get("reason", "")]


def test_when_the_tv_file_already_holds_the_name_the_dedupe_voids_it_and_keeps_the_uncut(lib):
    """v0.5.2 / v0.5.3 留下过的现场：TV 版已经改成了集位名。判重按偏好分留限制解除版、只作废 TV 那一个文件。

    仍未自动解决：作废的条目在种子里还叫集位名，qBittorrent 不许同一个种子里两个条目重名，限制解除版改不过去，
    要人把那个条目改个名字。但审计得说实话：以前照发 renameFile、撞 409、记成 unknown「也许生效了」（生产上连着
    17 轮）；现在不发请求，记一条写明原因的跳过，同一轮里也不重试。"""
    s1 = lib.show("学生会也有洞！").season(1)
    t = s1.torrent({SLOT: 495_339_739, UNCUT: 648_826_375}, name=BUNDLE, layout="nosub",
                   tags="ab:42, ma:S01E07", probe=CHI)
    uncut_ident = lib.ident(s1.path / UNCUT)

    run = lib.loop()

    [trash] = [r for r in lib.audit() if r["op"] == "trash"]
    assert trash["status"] == "applied" and trash["args"]["path"].endswith(SLOT)
    assert lib.disk() == {f"学生会也有洞！/Season 1/{UNCUT}": 648_826_375}
    assert lib.ident(s1.path / UNCUT) == uncut_ident
    assert run.report.unknown == [] and run.report.failed == []
    skips = [r for r in lib.audit() if r["op"] == "rename" and r["status"] == "skipped"
             and "不许重名" in r.get("reason", "")]
    assert len(skips) == 1 and not converge.retryable(skips[0])   # 一轮一条：没有动作会去挪不下载的条目，不重试
    assert skips[0]["holder"] == {"index": 0, "name": SLOT, "priority": 0}
    assert not [c for c in lib.qbit.calls if c[0] == "rename_file" and c[1] == t.hash]


def test_once_the_voided_entry_gives_up_the_name_the_next_run_renames_the_uncut_file(lib):
    """人工处理就是这一步（2026-11-26 生产上对 S01E08 做的）：把作废的条目改回别的名字，剩下的 agent 自己做完。"""
    s1 = lib.show("学生会也有洞！").season(1)
    t = s1.torrent({SLOT: 495_339_739, UNCUT: 648_826_375}, name=BUNDLE, layout="nosub",
                   tags="ab:42, ma:S01E07", probe=CHI)
    uncut_ident = lib.ident(s1.path / UNCUT)
    lib.loop()

    lib.qbit.rename_file(t.hash, SLOT, TV)

    lib.loop()
    assert lib.ident(s1.path / SLOT) == uncut_ident
    assert sorted(lib.qbit.file_names(t.hash)) == sorted([TV, SLOT])
    assert lib.loop().report.applied == []


@pytest.mark.parametrize("progress", [0.0, 1.0], ids=["not-downloaded", "downloaded"])
def test_a_live_entry_of_the_same_torrent_holding_the_name_is_still_a_retryable_occupied_slot(lib, progress):
    """上面那条跳过只管**不下载**的条目。还要下载的同种子条目照旧由占用闸门拦成「集位被占」——可重试：它自己这一轮
    可能被改走（PR #6 review 问的就是这个）。盘上有没有文件都一样。"""
    s1 = lib.show("学生会也有洞！").season(1)
    t = s1.torrent({SLOT: 495_339_739, UNCUT: 648_826_375}, name=BUNDLE, layout="nosub",
                   tags="ab:42", probe=CHI, progress=progress)

    def rename(src, dst):
        return Finding(rule="unrenamed-file", kind="unrenamed", severity="important",
                       summary=f"{src} → {dst}", show="学生会也有洞！", path=str(s1.path / src),
                       torrent_hash=t.hash,
                       action=Action(op="rename", args={"path": str(s1.path / src), "new_name": dst,
                                                        "torrent_hash": t.hash}))

    rep = lib.apply([rename(UNCUT, SLOT), rename(SLOT, TV)])        # 占着名字的那个排在后面才被改走

    [skip] = rep.skipped
    assert converge.retryable(skip) and "不许重名" not in skip["reason"]
    assert [r["args"]["new_name"] for r in rep.applied] == [TV]
    assert lib.apply([rename(UNCUT, SLOT)]).applied                 # 名字腾出来之后改得成
    assert sorted(lib.qbit.file_names(t.hash)) == sorted([TV, SLOT])


# ------------------------------------------------------------ 抓取的那一刻（2026-11-22 S01E08）
TV8 = "【10月】学生会也有洞！ 08【TV放送版】.mp4"
UNCUT8 = "【10月】学生会也有洞！ 08【限制解除版】.mp4"
SLOT8 = "学生会也有洞！ S01E08.mp4"
BUNDLE8 = "[TV版&无修版] 学生会也有洞！ - EP08 [简／繁] (1080p H.264 AAC SRTx2)"
SIZES8 = {TV8: 569_951_931, UNCUT8: 600_642_870}


def _grab_bundle(lib):
    """走真实的抓取动作：加种、钉 `ma:S01E08`、落进剧名分类。加完那一刻进度 0、盘上什么都没有。"""
    sh = lib.show("学生会也有洞！")
    sh.season(1)
    url, h = lib.web.torrent(BUNDLE8, files=dict(SIZES8))
    rep = lib.apply([Finding(
        rule="episode-available", kind="episode_grabbable", severity="important",
        summary="S01E08 可抓取", show=sh.name,
        action=Action(op="grab_episode", args={
            "url": url, "title": BUNDLE8, "show_dir": str(sh.path), "season": 1, "episode": 8,
            "bangumi_id": None, "category": sh.name, "official_title": sh.name}))])
    assert [r["op"] for r in rep.applied] == ["grab_episode"]
    return sh.path / "Season 1", h


def _occupied_or_unknown(lib):
    return [r for r in lib.audit() if r["op"] == "rename"
            and (r["status"] == "unknown" or "集位被占" in r.get("reason", ""))]


def test_right_after_the_grab_nothing_is_on_disk_yet_and_nothing_is_renamed(lib):
    """生产上的那一刻：06:46:00 加种，06:46:03 同一轮的下一次迭代。两个文件一个字节都还没写。"""
    s1, h = _grab_bundle(lib)
    assert sorted(lib.qbit.file_names(h)) == sorted(SIZES8)
    assert "ma:S01E08" in lib.qbit.torrent(h)["tags"]
    assert not any(os.path.lexists(s1 / n) or os.path.lexists(s1 / (n + ".!qB")) for n in SIZES8)

    c = lib.cycle()

    assert c.applied("rename") == [] and c.skipped("rename") == []
    assert sorted(lib.qbit.file_names(h)) == sorted(SIZES8)


@pytest.mark.parametrize("not_started", [TV, UNCUT], ids=["tv", "uncut"])
def test_when_only_one_file_has_started_downloading_nothing_is_renamed(lib, not_started):
    """下载是按块来的：一个文件已经有 `.!qB`、另一个还没开始写，是下载头几分钟的常态。"""
    s1, _t = _bundle(lib, progress=0.3)
    os.remove(s1.path / (not_started + ".!qB"))

    assert _renames(lib.diagnose()) == {}


def test_from_the_grab_to_the_finished_download_the_slot_name_goes_to_the_uncut_file(lib):
    s1, h = _grab_bundle(lib)
    lib.cycle()                                  # 抓取那一轮的下一次迭代
    lib.qbit.complete(h)
    uncut_ident = lib.ident(s1 / UNCUT8)

    run = lib.loop()                             # 下完之后的那一轮 run：判重作废 TV 版，下一次迭代改名

    assert [r["op"] for r in run.report.applied if r["op"] in ("trash", "rename")] == ["trash", "rename"]
    assert lib.ident(s1 / SLOT8) == uncut_ident
    assert lib.disk() == {f"学生会也有洞！/Season 1/{SLOT8}": SIZES8[UNCUT8]}
    assert sorted(lib.qbit.file_names(h)) == sorted([TV8, SLOT8])      # TV 条目留着原名、不下载
    assert _occupied_or_unknown(lib) == []
    assert lib.loop().report.applied == []       # 稳定了


def test_a_finished_sibling_gone_from_disk_is_voided_by_the_dedupe_and_the_other_is_renamed(lib):
    """v0.5.3 那个「盘上没有的不算一份」想防的情形：下完之后那一份被人删了。不用特殊对待——判重把丢了的那份作废
    （它当不了保留方），同一轮的下一次迭代另一份改名。"""
    s1, t = _bundle(lib)
    uncut_ident = lib.ident(s1.path / UNCUT)
    os.remove(s1.path / TV)

    run = lib.loop()

    assert [r["op"] for r in run.report.applied if r["op"] in ("trash", "rename")] == ["trash", "rename"]
    [trash] = [r for r in run.report.applied if r["op"] == "trash"]
    assert trash["args"]["path"].endswith(TV)
    assert lib.ident(s1.path / SLOT) == uncut_ident
    assert sorted(lib.qbit.file_names(t.hash)) == sorted([TV, SLOT])
    assert lib.loop().report.applied == []
