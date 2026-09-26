"""抓取后的即时改名（`_rename_grabbed` → `grabber.rename_single_video`）走占用闸门。

以前它加完种子就把唯一的正片 `renameFile` 到 `{标题} SxxEyy.ext`，**什么都不查**。
新种子刚拿到元数据、一个字节都没下，libtorrent 只改映射——目标名此刻若被别的种子
声明着（或盘上有别人的 `X.!qB`），两个种子就无声地宣称同一路径。这正是
2026-09-06 尼古喵喵 S01E08 丢片的形态：两个种子声明同一路径，scan 只出一条，判重把
唯一的真文件当输家清走。grab 调研 §4 列出的两种入口：

- **S3 与 AutoBangumi 赛跑**：诊断之后、加种之前 AB 下完并改名到 X。新种子被映射到
  X，完成时 `X.!qB → X` 撞 EEXIST，偏好的版本作为孤儿半成品静默留下。
- **S1 停滞放行换源**：旧种子停滞够久，抓取（op 0）放行新源，而摘旧种子是 op 1——
  改名那一刻旧种子还声明着 X，盘上还有它的 `X.!qB`。

被占时：不改名、保留 `ma:` 钉子、写明原因。收敛靠已有的流程：新种子下完后
duplicate-episode 凭钉子封存它、把占位的清进隔离区（op 5），同一轮 unrenamed-file
再把它改到集位名上（op 6）。

另有 N15：`rename_single_video` 算目标名时丢掉条目的文件夹层（`_op_rename` 保留）。
409 撞上一个已有的 Original 布局种子时，文件被挪到 save_path 根下。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness import MikanItem, weekly
from media_agent.claims import ClaimIndex
from media_agent.grabber import rename_single_video
from media_agent.kernel import Action, Finding

GB = 600_000_000
SHOW = "尼古喵喵"
TITLE = "[LoliHouse] 尼古喵喵 / Yani Neko - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
RELEASE = TITLE.replace("/", "_") + ".mkv"          # libtorrent 规范化后的单文件名
SLOT = "尼古喵喵 S01E12.mkv"


def grab_finding(show_dir: Path, url: str, *, ep: int = 12, title: str = TITLE) -> Finding:
    return Finding(rule="episode-available", kind="episode_grabbable", severity="important",
                   summary=f"S01E{ep:02d} 可抓取", show=show_dir.name,
                   action=Action(op="grab_episode", args={
                       "url": url, "title": title, "show_dir": str(show_dir),
                       "season": 1, "episode": ep, "bangumi_id": None,
                       "category": show_dir.name, "official_title": show_dir.name}))


def _renames_of(lib, h) -> list[tuple]:
    return [c for c in lib.qbit.calls if c[0] == "rename_file" and c[1] == h]


def _grab_logs(lib) -> list[str]:
    return [line for line in lib.logs if line.startswith("[grab]")]


# ------------------------------------------------------------------ 被占就不改
def test_grab_rename_onto_a_slot_another_torrent_claims_is_skipped(lib):
    """0% 的另一个种子已映射到集位名：盘上什么都没有，以前照改。"""
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    other = s1.single(SLOT, size=GB, progress=0.0, name="[Other] Yani Neko - 12.mkv")
    url, h = lib.web.torrent(TITLE)

    rep = lib.apply([grab_finding(sh.path, url)])

    [rec] = rep.applied
    assert rec["op"] == "grab_episode"
    assert rec["rename"]["renamed"] is None
    assert [c["hash"] for c in rec["rename"]["claims"]["claimants"]] == [other.hash]
    assert _renames_of(lib, h) == []
    assert lib.qbit.file_names(h) == [RELEASE]            # 留在发布名上
    assert "ma:S01E12" in lib.qbit.torrent(h)["tags"]    # 钉子还在，判重认得出它
    [log] = _grab_logs(lib)
    assert "不改名" in log and other.hash[:8] in log
    assert 12 in lib.sidecar(SHOW).seasons["1"]["have"]  # 记账照常


def test_grab_rename_onto_an_existing_file_is_skipped(lib):
    """S3 的落地形态：AB 的那份已经下完、叫集位名，盘上有、种子也声明着。"""
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    ab = s1.single(SLOT, size=GB, name="[ANi] Yani Neko - 12 [1080P].mkv", category="Bangumi")
    ident = lib.ident(ab.path)
    url, h = lib.web.torrent(TITLE)

    rep = lib.apply([grab_finding(sh.path, url)])

    [rec] = rep.applied
    kinds = sorted(c["kind"] for c in rec["rename"]["claims"]["claimants"])
    assert kinds == ["disk", "qbit"]
    assert _renames_of(lib, h) == [] and lib.qbit.file_names(h) == [RELEASE]
    assert lib.ident(s1.path / SLOT) == ident


def test_grab_rename_onto_an_orphan_partial_is_skipped(lib):
    """盘上只有一份没人认领的 `X.!qB`（死种摘了记录留下的）：新种子会接着往里写。"""
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    dead = s1.single(SLOT, size=GB, progress=0.4, name="[Dead] Yani Neko - 12.mkv")
    lib.qbit.delete([dead.hash], delete_files=False)
    url, h = lib.web.torrent(TITLE)

    rep = lib.apply([grab_finding(sh.path, url)])

    [rec] = rep.applied
    [c] = rec["rename"]["claims"]["claimants"]
    assert c["kind"] == "disk" and c["partial"] is True
    assert lib.qbit.file_names(h) == [RELEASE]


@pytest.mark.allow("log_failure", match=r"^\[grab\]")
def test_grab_rename_is_refused_when_occupancy_cannot_be_read(lib):
    """同目录另一个种子的文件列表读不到：它可能正声明着集位名。抓取照常记账，只是不改名。"""
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    neighbour = s1.single("尼古喵喵 S01E11.mkv", size=GB)
    lib.qbit.fail("files", hash=neighbour.hash, times=None)
    url, h = lib.web.torrent(TITLE)

    rep = lib.apply([grab_finding(sh.path, url)])

    [rec] = rep.applied
    assert rec["rename"]["renamed"] is None
    assert neighbour.hash[:8] in rec["rename"]["claims"]["unknown"]
    assert _renames_of(lib, h) == [] and lib.qbit.file_names(h) == [RELEASE]


def test_grab_rename_when_the_slot_is_free_still_happens(lib):
    sh = lib.show(SHOW)
    sh.season(1)
    url, h = lib.web.torrent(TITLE)

    rep = lib.apply([grab_finding(sh.path, url)])

    [rec] = rep.applied
    assert rec["rename"] == {"renamed": SLOT}
    assert lib.qbit.file_names(h) == [SLOT]
    assert _grab_logs(lib) == []


# ------------------------------------------------------------------ N15：保留文件夹层
def test_grab_409_on_an_original_layout_torrent_keeps_its_folder(lib):
    """同一个种子已经以 Original 布局在 qBittorrent 里（别处加的）：409，照样改名，
    但只改文件名，不把它从文件夹里挪到 save_path 根下。"""
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    title = "[LoliHouse] Yani Neko - 12 [WebRip 1080p HEVC-10bit AAC]"
    url, h = lib.web.torrent(title, files={"Yani Neko - 12.mkv": GB, "readme.txt": 100})
    blob = lib.web.urlopen(url).read()
    lib.qbit.add_torrent(blob, save_path=str(s1.path), category=SHOW, no_subfolder=False)

    rep = lib.apply([grab_finding(sh.path, url, title=title)])

    [rec] = rep.applied
    assert rec["already_present"] is True
    assert rec["rename"]["renamed"] == f"{title}/{SLOT}"
    assert lib.qbit.file_names(h) == [f"{title}/{SLOT}", f"{title}/readme.txt"]


def test_rename_single_video_keeps_the_folder_and_builds_its_own_index(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.torrent({"Yani Neko - 12.mkv": GB, "Yani Neko - 12.ass": 10},
                   name="[G] Yani Neko 12", layout="original", progress=0.0)

    out = rename_single_video(lib.qbit, t.hash, "尼古喵喵 S01E12")

    assert out.renamed == "[G] Yani Neko 12/尼古喵喵 S01E12.mkv"
    assert lib.qbit.file_names(t.hash)[0] == out.renamed


def test_rename_single_video_case_only_change_of_own_file_is_allowed(lib):
    s1 = lib.show(SHOW).season(1)
    t = s1.single("尼古喵喵 s01e12.mkv", size=GB)

    out = rename_single_video(lib.qbit, t.hash, "尼古喵喵 S01E12",
                              claims=ClaimIndex(lib.qbit))

    assert out.renamed == SLOT and lib.qbit.file_names(t.hash) == [SLOT]


# ------------------------------------------------------------------ 端到端收敛
def _season(lib, *, allow_empty: bool = False):
    if allow_empty:
        lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 12):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=80)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", seasons={"1": {"have": list(range(1, 12))}})
    item = MikanItem(title=TITLE, pub=dict(schedule)[12])
    lib.mikan("3500", [item], search=[SHOW])
    return sh, s1, item


def _claims_of(lib, path: Path) -> list[str]:
    return sorted(h for h in lib.qbit.snapshot()
                  if path in [Path(lib.qbit.raw(h)["save_path"]) / n
                              for n in lib.qbit.file_names(h)])


def test_race_with_autobangumi_is_skipped_at_grab_time_and_converges_later(lib):
    """S3 原样：诊断时 E12 缺着，加种之前 AB 下完并改名到集位名。

    抓取不改名（钉子留着）；新种子下完后，duplicate-episode 凭钉子封存它、把 AB 那份
    清进隔离区（op 5），同一轮 unrenamed-file 把它改到集位名上（op 6）。最后只有它
    一个种子声明集位名，AB 那份在隔离区里、可回退。
    """
    sh, s1, item = _season(lib, allow_empty=True)
    ctx = lib.context()
    findings = lib.diagnose(ctx=ctx)
    assert [f.action.args["episode"] for f in findings
            if f.action and f.action.op == "grab_episode"] == [12]
    # —— 赛跑：AB 在诊断与执行之间下完、改好了名 ——
    ab = s1.single(SLOT, size=GB + 7, name="[ANi] Yani Neko - 12 [1080P][Baha][WEB-DL].mkv",
                   category="Bangumi")
    ab_ident = lib.ident(ab.path)

    rep = lib.apply(findings, ctx=ctx, run_id="grab")

    [grab] = [r for r in rep.applied if r["op"] == "grab_episode"]
    assert grab["rename"]["renamed"] is None
    assert ab.hash in [c.get("hash") for c in grab["rename"]["claims"]["claimants"]]
    assert _claims_of(lib, s1.path / SLOT) == [ab.hash]
    assert lib.qbit.file_names(item.infohash) == [RELEASE]

    lib.qbit.complete(item.infohash)                     # 新种子下完了
    ours = lib.ident(s1.path / RELEASE)
    rounds = lib.converge()

    first = rounds[0]
    [evict] = [r for r in first.applied("trash") if r["args"]["path"] == str(s1.path / SLOT)]
    assert evict["rule"] == "duplicate-episode" and "封存" in evict["summary"]
    assert [r["args"]["torrent_hash"] for r in first.applied("rename")] == [item.infohash]
    assert _claims_of(lib, s1.path / SLOT) == [item.infohash]
    assert lib.ident(s1.path / SLOT) == ours
    assert lib.ident(Path(evict["trashed_to"])) == ab_ident
    assert "ma:S01E12" in lib.qbit.torrent(item.infohash)["tags"]
    assert not any(r.failed() for r in rounds)


def test_grab_rename_skip_without_a_race_never_happens_in_a_plain_cycle(lib):
    """反向守卫：集位空着时，一轮完整流程里抓取当场改好名（2026-09-03 的回归不能回来）。"""
    sh, s1, item = _season(lib, allow_empty=True)

    c = lib.cycle()

    [grab] = c.applied("grab_episode")
    assert grab["rename"] == {"renamed": SLOT}
    assert lib.qbit.file_names(item.infohash) == [SLOT]
