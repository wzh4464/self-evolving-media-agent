"""死种（dead-torrent）只能处置**它自己**，而且要真的"死了"。

testinfra B1 / critic N7：`DeadTorrentDetector` 把 `content_path` 当作要移进隔离区的
路径。NoSubfolder 布局的多文件种子（本项目自己抓的种子默认就是这个布局，生产上
10 个）`content_path == save_path`，也就是整个 `Season N` 目录——一个死掉的
`[TV版&无修版] 尼古喵喵 - EP11` 会把整季（12 个种子的文件、所有封存集位）一起
搬进隔离区，而目录的 `st_size` 只有几百字节，体积配额拦不住。同一目录下两个
死种的 finding 还会因为 `key()` 相同而塌成一条。

判死用的是 `added_on`（加入多久），不是停滞多久：一个半年前加的、一小时前还在
收数据的种子也会被判死。
"""
from __future__ import annotations

import time

import pytest

from media_agent.plugins.builtin import DeadTorrentDetector

GB = 600_000_000


def _dead(d, files, name, **kw):
    kw.setdefault("progress", 0.4)
    kw.setdefault("state", "stalledDL")
    kw.setdefault("added_hours_ago", 24 * 30)
    kw.setdefault("availability", 0)
    kw.setdefault("num_complete", 0)
    return d.torrent(files, name=name, **kw)


def _nosub_season(lib):
    s1 = lib.show("尼古喵喵").season(1)
    healthy = s1.torrent({"尼古喵喵 S01E01.mkv": GB, "尼古喵喵 S01E02.mkv": GB},
                         name="[Group] Yani Neko 01-02", layout="nosub")
    lone = s1.single("尼古喵喵 S01E03.mkv", name="[LoliHouse] Yani Neko - 03.mkv")
    dead = _dead(s1, {"尼古喵喵 S01E11.mkv": GB, "尼古喵喵 S01E12.mkv": GB},
                 "[TV版&无修版] 尼古喵喵 - EP11-12", layout="nosub")
    assert dead.view()["content_path"] == str(s1.path)       # 前提：content_path 就是季目录
    return s1, healthy, lone, dead


def test_dead_nosubfolder_torrent_never_trashes_the_season(lib):
    s1, healthy, lone, dead = _nosub_season(lib)
    before = lib.disk()
    idents = {p: lib.ident(lib.path(p)) for p in before}

    rounds = lib.converge()

    assert lib.trash_files() == []
    assert lib.disk() == before                               # 连死种自己的 .!qB 都不动
    assert {p: lib.ident(lib.path(p)) for p in before} == idents
    assert lib.qbit.has(healthy.hash) and lib.qbit.has(lone.hash)
    assert not lib.qbit.has(dead.hash)                        # 只摘死种自己的记录
    [drop] = rounds[0].applied("drop_torrent")
    assert drop["args"]["torrent_hash"] == dead.hash
    assert drop["undo"]["op"] == "readd_torrent"


def test_two_dead_torrents_in_one_season_are_both_handled(lib):
    """N7：两个死种 content_path 相同，旧的去重键会把第二条吞掉。"""
    s1 = lib.show("尼古喵喵").season(1)
    a = _dead(s1, {"尼古喵喵 S01E11.mkv": GB, "NCOP.mkv": 90_000_000},
              "[A] EP11", layout="nosub")
    b = _dead(s1, {"尼古喵喵 S01E12.mkv": GB, "NCED.mkv": 90_000_000},
              "[B] EP12", layout="nosub")

    c = lib.cycle(detectors=[DeadTorrentDetector])

    assert sorted(r["args"]["torrent_hash"] for r in c.applied("drop_torrent")) == \
        sorted([a.hash, b.hash])
    assert lib.trash_files() == []


def test_old_but_recently_active_torrent_is_not_dead(lib):
    """加入 30 天，但一小时前还在收数据：它在动，不是死种。"""
    s1 = lib.show("尼古喵喵").season(1)
    _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single",
          active_hours_ago=1)

    assert lib.diagnose(detectors=[DeadTorrentDetector]) == []


def test_recently_seen_complete_is_not_dead(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    lib.qbit.raw(t.hash)["seen_complete"] = int(time.time() - 3600)

    assert lib.diagnose(detectors=[DeadTorrentDetector]) == []


def test_long_stalled_single_file_torrent_is_dead(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single",
              active_hours_ago=24 * 5)

    [f] = lib.diagnose(detectors=[DeadTorrentDetector])
    assert f.torrent_hash == t.hash and f.action.op == "drop_torrent"
    assert f.show == "尼古喵喵"


def test_torrents_outside_the_library_are_not_ours(lib):
    """生产 2026-09-08：dead-torrent 删了 Media/.staging/opm-oad/ 下三个手动种子。

    库外那个种子必须真的"死够了"（加入 30 天、从没动过），否则它本来就不会被判死，
    这半条测试就是空转——以前 `added_on=0` 让 `last_sign_of_life` 退回 `now`，
    停滞 0 小时，删掉"只管媒体库"的前缀判断测试照样绿。"""
    staging = lib.show(".staging").folder("opm-oad")
    _dead(staging, {"OPM OAD.mkv": GB}, "OPM OAD.mkv", layout="single")
    month_ago = time.time() - 30 * 86400
    lib.qbit.seed("f" * 40, name="elsewhere", save_path=lib.root / "downloads",
                  files={"x.mkv": GB}, progress=0.2, state="stalledDL",
                  added_on=month_ago, num_complete=0, availability=0)

    assert lib.diagnose(detectors=[DeadTorrentDetector]) == []


def test_the_outside_torrent_above_would_be_dead_if_it_were_ours(lib):
    """对照组：同样的种子放进媒体库，就是死种——上一条测试的"不管"才有意义。"""
    s1 = lib.show("尼古喵喵").season(1)
    lib.qbit.seed("f" * 40, name="elsewhere", save_path=s1.path,
                  files={"x.mkv": GB}, progress=0.2, state="stalledDL",
                  added_on=time.time() - 30 * 86400, num_complete=0, availability=0)

    [f] = lib.diagnose(detectors=[DeadTorrentDetector])
    assert f.torrent_hash == "f" * 40


def test_dead_pack_with_completed_members_is_only_reported(lib):
    """死种里已经下完的成员文件是可播的正片：摘记录会让它们失去做种，交给人。"""
    s1 = lib.show("银八").season(1)
    t = _dead(s1, {"银八 S01E01.mkv": GB, "银八 S01E02.mkv": GB}, "[G] Gintama 01-02",
              layout="nosub")
    raw = lib.qbit.raw(t.hash)
    raw["_files"][0]["progress"] = 1

    c = lib.cycle(detectors=[DeadTorrentDetector])

    [f] = c.findings
    assert f.kind == "dead_torrent" and f.action is None
    assert lib.qbit.has(t.hash)


def test_dead_drop_revalidates_live_state(lib):
    """诊断后、执行前它又有了做种：不再是死种，跳过。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    findings = lib.diagnose(detectors=[DeadTorrentDetector])
    lib.qbit.raw(t.hash)["num_complete"] = 3

    rep = lib.apply(findings)

    assert not rep.applied and [r["op"] for r in rep.skipped] == ["drop_torrent"]
    assert lib.qbit.has(t.hash)


def test_dead_drop_rechecks_completed_members_at_execution_time(lib):
    """诊断时没有已下完的成员，执行前有一个下完了：它是可播的正片、还在做种，不摘。
    （检测器总是先把这种情况滤掉，执行器这道复核以前从没被走到过。）"""
    s1 = lib.show("银八").season(1)
    t = _dead(s1, {"银八 S01E01.mkv": GB, "银八 S01E02.mkv": GB}, "[G] Gintama 01-02",
              layout="nosub")
    findings = lib.diagnose(detectors=[DeadTorrentDetector])
    assert [f.action.op for f in findings] == ["drop_torrent"]
    lib.qbit.raw(t.hash)["_files"][0]["progress"] = 1

    rep = lib.apply(findings)

    [skip] = rep.skipped
    assert "已下完" in skip["reason"]
    assert not rep.applied and lib.qbit.has(t.hash)


def test_detector_draws_no_conclusion_when_it_cannot_read_the_file_list(lib):
    """"看不到文件列表就不下结论"：files() 出错不能当成"没有已下完的成员"。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    state = lib.scan(resolve_tmdb=False)                  # 扫描时一切正常
    lib.qbit.fail("files", hash=t.hash, times=None)       # 诊断时这个种子的 files() 超时

    assert lib.diagnose(state, detectors=[DeadTorrentDetector]) == []


def test_future_activity_timestamp_falls_back_to_added_on(lib):
    """`last_activity` 在未来（时钟错乱 / 字段语义不符）：不能让它把死种"续命"，
    退回按加入时间算（`last_sign_of_life` 的 `<= now + 60` 上界）。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    lib.qbit.raw(t.hash)["last_activity"] = int(time.time() + 10 * 86400)

    [f] = lib.diagnose(detectors=[DeadTorrentDetector])
    assert f.torrent_hash == t.hash and f.evidence["stalled_hours"] >= 24 * 29


def test_dead_drop_rolls_back_with_the_same_layout(lib):
    s1, healthy, lone, dead = _nosub_season(lib)
    names = lib.qbit.file_names(dead.hash)

    c = lib.cycle(detectors=[DeadTorrentDetector])
    assert not lib.qbit.has(dead.hash)
    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert lib.qbit.file_names(dead.hash) == names          # NoSubfolder 没长出根目录
    assert lib.qbit.torrent(dead.hash)["save_path"] == str(s1.path)


def test_dead_drop_of_one_file_in_a_folder_rolls_back_with_the_folder(lib):
    """Original 布局、只有一集装在根文件夹里：真 qBit 报 root_path，回退按 Original
    加回来，文件夹还在（以前 FakeQbit 报空 root_path，这条生产上正确的路径在基座里是错的）。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"Yani Neko - 11.mkv": GB}, "[G] Yani Neko - 11", layout="original")
    names = lib.qbit.file_names(t.hash)
    assert names == ["[G] Yani Neko - 11/Yani Neko - 11.mkv"]

    c = lib.cycle(detectors=[DeadTorrentDetector])
    [drop] = c.applied("drop_torrent")
    assert drop["undo"]["no_subfolder"] is False
    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert lib.qbit.file_names(t.hash) == names


@pytest.mark.parametrize("hours", [47, 49])
def test_threshold_is_measured_from_last_activity(lib, hours):
    s1 = lib.show("尼古喵喵").season(1)
    _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single",
          active_hours_ago=hours)

    found = lib.diagnose(detectors=[DeadTorrentDetector])
    assert bool(found) is (hours >= 48)
