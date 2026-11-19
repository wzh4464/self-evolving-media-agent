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
"""
from __future__ import annotations

import pytest

from harness import video

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


@pytest.mark.allow("unknown_record", "qbit_error")      # 见 docstring 的「已知未解决」
def test_when_the_tv_file_already_holds_the_name_the_dedupe_voids_it_and_keeps_the_uncut(lib):
    """生产上 v0.5.2 留下的现场：TV 版已经改成了集位名。判重按偏好分留限制解除版、只作废 TV 那一个文件。

    已知未解决：作废的条目在种子里仍叫集位名，qBittorrent 不许同一个种子里两个条目重名，限制解除版随后的
    改名会报 `unknown`。修好之后新的合并发布不会再走到这一步（TV 版拿不到集位名）；生产那一份已手工对调。"""
    s1 = lib.show("学生会也有洞！").season(1)
    s1.torrent({SLOT: 495_339_739, UNCUT: 648_826_375}, name=BUNDLE, layout="nosub",
               tags="ab:42, ma:S01E07", probe=CHI)
    uncut_ident = lib.ident(s1.path / UNCUT)

    lib.cycle()

    [trash] = [r for r in lib.audit() if r["op"] == "trash"]
    assert trash["status"] == "applied" and trash["args"]["path"].endswith(SLOT)
    assert lib.disk() == {f"学生会也有洞！/Season 1/{UNCUT}": 648_826_375}
    assert lib.ident(s1.path / UNCUT) == uncut_ident
