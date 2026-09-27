"""抓取模式（`grabmode.run`，`media-agent grab` 用它）：只补缺的集、并把刚抓的那一集收尾。

AB 每 15 分钟拉 RSS、60 秒改名；`run` 每 6 小时。AB 退役之后，新集最坏晚 6 小时才抓、刚抓的多文件发布要等 6 小时才改名
（ab 调研 §5.1 / §5.2）。抓取模式每 30 分钟一次，**与 `run` 同一套检测器与执行器**（`converge.run`，一轮一个执行器、试过的
不再试、反向拒绝），只是挑着做：

- 接手 AB 订阅（`ab-adoption`：建目录、登记订阅、迁偏移）——AB 里刚订的新番 30 分钟之内开始抓；
- 抓（`grab_episode`）——但**不换源**：旧种子死了、换一个发布，要同一批摘掉旧种子（dead-torrent），那是 `run` 的事；
- 改名，只改本项目抓的种子（`ma:` 钉子 / 出处账本的抓取行）的文件——AB 的它自己 60 秒就改；
- 判重，只在集位里有本项目抓的那一份时（抓取补上的集位与 AB 的重复）——删除照样过删除关口。
其余治理（标题对齐、分类、死种、特典、NFO、隔离区处置……）留给 6 小时的 `run`。
"""
from __future__ import annotations

from harness import MikanItem, video, weekly

from media_agent import converge, grabmode

SHOW = "抓取戊"
TMDB = 3701
MID = "4901"
TPL = "[LoliHouse] 抓取戊 / Zhuaqu Wu - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def _airing(lib, *, have=(1, 2), releases=(5,)):
    """在播的第一季（第 1–5 集已播）；盘上有 `have` 那几集。"""
    sched = weekly(12, first_days_ago=30)
    lib.tmdb.add_show(TMDB, SHOW, seasons={1: sched})
    sh = lib.show(SHOW)
    for n in have:
        sh.season(1).local(f"{SHOW} S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=TMDB, tmdb_source="human", tmdb_title=SHOW, mikan_id=MID,
               seasons={"1": {"have": list(have)}})
    lib.mikan(MID, [MikanItem(title=TPL.format(n), pub=dict(sched)[n]) for n in releases], search=[SHOW])
    return sh


def test_only_accepts_per_op_guards():
    from media_agent.kernel import Action, Finding

    pick = converge.only("grab_episode", rename=lambda f: f.torrent_hash == "a")

    def f(op, h=""):
        return Finding(rule="r", kind="k", severity="minor", summary="s", torrent_hash=h,
                       action=Action(op=op, args={}))

    assert pick(f("grab_episode")) and pick(f("rename", "a"))
    assert not pick(f("rename", "b")) and not pick(f("trash", "a"))
    assert not pick(Finding(rule="r", kind="k", severity="minor", summary="s"))


def test_grab_mode_grabs_and_renames_only_what_media_agent_grabbed(lib):
    sh = _airing(lib)
    s1 = sh.season(1)
    ours = s1.single("[LoliHouse] Zhuaqu Wu - 03 [WebRip 1080p].mkv", tags="ma:S01E03", probe=TWO_SUBS)
    ab = s1.single("[ANi] Zhuaqu Wu - 04 [1080P][Baha][WEB-DL][CHT].mp4", tags="ab:71", probe=TWO_SUBS)

    loop = lib.grab_loop()

    assert [r["args"]["episode"] for r in loop.applied("grab_episode")] == [5]
    renamed = {r["args"]["torrent_hash"] for r in loop.applied("rename")}
    assert renamed == {ours.hash}
    assert not [r for r in lib.audit() if (r.get("args") or {}).get("torrent_hash") == ab.hash]
    # AB 的那一份照样被诊断出来（`run` 会改），只是抓取模式不碰
    assert any(f.action and f.action.op == "rename" and f.torrent_hash == ab.hash for f in loop.findings)


def test_grab_mode_renames_a_torrent_the_ledger_says_it_grabbed(lib):
    """"本项目抓的" = `ma:` 钉子**或**出处账本里的抓取行：钉子没了（人改过标签）的，账本照样认，照样收尾改名。以前每个
    抓取模式的测试都靠 `ma:` 认（审查的变异 T4-05 删掉账本那一半，全套测试照样过）。"""
    from media_agent import ledger

    sh = _airing(lib, have=(1, 2, 4))
    ours = sh.season(1).single("[LoliHouse] Zhuaqu Wu - 03 [WebRip 1080p].mkv", probe=TWO_SUBS)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=ours.hash, mikan_title=TPL.format(3), season=1, episode=3, run_id="g0")

    loop = lib.grab_loop()

    assert {r["args"]["torrent_hash"] for r in loop.applied("rename")} == {ours.hash}
    assert [p.name for p in ours.current_paths()] == [f"{SHOW} S01E03.mkv"]


def test_the_scope_keeps_torrents_it_handed_over_in_earlier_iterations(lib):
    """订阅模式：这一轮见过在 AB 分类里的种子，交接到剧名分类之后照样算"本项目的"（下一次迭代改名、判重还认它）。
    审查的变异 W2-21（`handed |=` 写成 `handed =`）全套测试照样过——交接的测试在同一次迭代里就改完了名。"""
    from types import SimpleNamespace

    lib.configure(ab_mode="subscription")
    scope = grabmode.Scope(lib.cfg)
    h = "a" * 40

    scope.observe(SimpleNamespace(torrents=[{"hash": h.upper(), "category": "Bangumi", "tags": ""}], ledger_rows={}))
    assert h in scope.grabbed
    scope.observe(SimpleNamespace(torrents=[{"hash": h, "category": SHOW, "tags": ""}], ledger_rows={}))
    assert h in scope.grabbed
    assert "b" * 40 not in scope.grabbed


def test_grab_mode_dedupes_a_slot_that_holds_its_own_grab(lib):
    """AB 的 ABEMA 生肉占着 E04 的集位名；本项目抓的 LoliHouse 那份下完了：判重清走生肉、赢家改名——同一次抓取里收尾。"""
    sh = _airing(lib, releases=())
    s1 = sh.season(1)
    raw = s1.single(f"{SHOW} S01E04.mkv", name="[Dynamis One] Zhuaqu Wu - 04 (ABEMA 1920x1080 AVC AAC MKV).mkv",
                    size=745_065_995, probe=video("h264"))
    ours = s1.single("[LoliHouse] Zhuaqu Wu - 04 [WebRip 1080p].mkv", tags="ma:S01E04",
                     size=593_601_176, probe=TWO_SUBS)

    loop = lib.grab_loop()

    [trash] = loop.applied("trash")
    assert trash["args"]["torrent_hash"] == raw.hash and trash["args"]["keep_hash"] == ours.hash
    [ren] = loop.applied("rename")
    assert ren["args"]["torrent_hash"] == ours.hash


def test_grab_mode_leaves_a_duplicate_without_its_own_grab_to_run(lib):
    sh = _airing(lib, releases=())
    s1 = sh.season(1)
    s1.single(f"{SHOW} S01E04.mkv", name="[Dynamis One] Zhuaqu Wu - 04 (ABEMA 1920x1080 AVC AAC MKV).mkv",
              size=745_065_995, probe=video("h264"))
    s1.single("[LoliHouse] Zhuaqu Wu - 04 [WebRip 1080p].mkv", size=593_601_176, probe=TWO_SUBS)

    loop = lib.grab_loop()

    assert not loop.report.applied
    assert any(f.action and f.action.op == "trash" for f in loop.findings)       # `run` 会做


def test_grab_mode_does_not_replace_a_dead_torrent(lib):
    """E03 的旧种子死了（72 小时没动静、0 做种）：`run` 会换源、同一批摘掉它；抓取模式不换——摘死种不在它的范围里，
    换了就是两个种子抢一个集位。"""
    sh = _airing(lib, have=(1, 2, 4, 5), releases=(3,))
    sh.season(1).torrent({f"{SHOW} S01E03.mkv": 600_000_000}, name="[Old] Zhuaqu Wu - 03.mkv", layout="single",
                         progress=0.4, state="stalledDL", added_hours_ago=72, num_complete=0, availability=0,
                         tags="ma:S01E03")

    loop = lib.grab_loop()

    assert not loop.applied("grab_episode")
    full = lib.loop()                                   # 对照：完整的一轮换源、摘旧种
    assert [r["args"]["episode"] for r in full.applied("grab_episode")] == [3]
    assert full.applied("drop_torrent")


def test_grab_mode_picks_up_a_new_ab_subscription(lib):
    """AB 里刚订的新番：抓取模式里就建目录、登记订阅，下一次迭代开始抓（不用等 6 小时的 run）。"""
    sched = weekly(12, first_days_ago=10)
    lib.tmdb.add_show(3702, "新番己", seasons={1: sched})
    lib.bangumi(id=72, official_title="新番己", title_raw="Shinban Ki", season=1,
                rss_link="https://mikanani.me/RSS/Bangumi?bangumiId=4902&subgroupid=1",
                save_path=str(lib.media_root / "新番己" / "Season 1"))
    tpl = "[LoliHouse] 新番己 / Shinban Ki - {:02d} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    lib.mikan("4902", [MikanItem(title=tpl.format(n), pub=dict(sched)[n]) for n in (1, 2)], search=["新番己"])

    loop = lib.grab_loop()

    assert loop.applied("create_show_dir")
    assert sorted(r["args"]["episode"] for r in loop.applied("grab_episode")) == [1, 2]


def test_grab_mode_does_no_other_governance(lib):
    """标题对齐、NFO、sidecar 同步都是 `run` 的事：抓取模式的诊断里根本没有这些规则。"""
    names = {getattr(d, "id", "") for d in grabmode.registry().detectors}
    assert names == {"ab-adoption", "episode-available", "duplicate-episode", "unrenamed-file"}


# ------------------------------------------------------------------ 订阅模式：AB 订阅那一刻补的集，抓取当场接手
ANI_04 = "[ANi] Zhuaqu Wu - 04 [1080P][Baha][WEB-DL][CHT].mp4"


def test_subscription_mode_grab_hands_over_what_ab_collected_at_subscribe_time(lib):
    """订阅模式（`AB_MODE=subscription`）下 AB 的改名线程停了：人在 AB 里订阅的那一刻它把已发布的集补进 `Bangumi`
    分类，之后再没人改名。抓取当场交接（分类改成剧名）、改名、判重——不等 6 小时的 `run`。别的分类碎片、删空分类仍是
    `run` 的事。"""
    lib.configure(ab_mode="subscription")
    sh = _airing(lib, releases=())
    s1 = sh.season(1)
    ab = s1.single(ANI_04, category="Bangumi", probe=TWO_SUBS)
    frag = s1.single("[G] Zhuaqu Wu - 03 [1080p].mkv", category="抓取戊（旧译名）", probe=TWO_SUBS)

    loop = lib.grab_loop()

    assert lib.qbit.torrent(ab.hash)["category"] == SHOW
    assert [p.name for p in ab.current_paths()] == [f"{SHOW} S01E04.mp4"]
    assert lib.qbit.torrent(frag.hash)["category"] == "抓取戊（旧译名）"
    assert not loop.applied("delete_category")
    assert {r["args"]["torrent_hash"] for r in loop.applied("recategorize")} == {ab.hash}


def test_full_mode_grab_leaves_the_bangumi_category_to_ab(lib):
    sh = _airing(lib, releases=())
    ab = sh.season(1).single(ANI_04, category="Bangumi", probe=TWO_SUBS)

    loop = lib.grab_loop()

    assert not loop.report.applied
    assert lib.qbit.torrent(ab.hash)["category"] == "Bangumi"
    assert "category-consolidation" not in {getattr(d, "id", "") for d in grabmode.registry(lib.cfg).detectors}
