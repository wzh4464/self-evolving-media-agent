"""死种被摘记录之后，它**自己的** `.!qB` 半成品经删除关口移进隔离区。

第 1 阶段把死种处置收窄成"只摘记录、磁盘一个字节都不动"（以前 `content_path` 是整个
Season 目录，见 `tests/test_dead_torrent.py`）。代价是半成品留在盘上：
- 占空间——隔离区与媒体在同一个 94% 满的 APFS 容器里（critic N8）；
- 更糟的是**占着集位名**：停滞换源时新种子下完也改不过去，占用闸门按设计拦下
  （改过去就是接着往那份半成品里写）——`tests/test_grab_stale_bypass.py` 里那条 xfail。

处置保持保守：只动死种自己的、没下完的、优先级非 0 的条目在盘上的 `X.!qB`；死种必须在
**同一批里真的被摘掉**（摘除被跳过——又有了做种、有已下完的成员——半成品就不动）；另一个
种子仍声明着 `X` 时不动（I2）。进隔离区、可回退，不是删除。
"""
from __future__ import annotations

from media_agent import gate
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import DeadTorrentDetector

GB = 600_000_000


def _dead(d, files, name, **kw):
    kw.setdefault("progress", 0.4)
    kw.setdefault("state", "stalledDL")
    kw.setdefault("added_hours_ago", 24 * 30)
    kw.setdefault("availability", 0)
    kw.setdefault("num_complete", 0)
    return d.torrent(files, name=name, **kw)


def test_dead_torrents_own_partial_goes_to_trash_after_the_drop(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    partial = s1.path / "尼古喵喵 S01E11.mkv.!qB"
    ident = lib.ident(partial)

    c = lib.cycle(detectors=[DeadTorrentDetector])

    [drop] = c.applied("drop_torrent")
    [rec] = c.applied("trash")
    assert rec["args"]["path"] == str(partial)
    assert rec["deletion"]["gate"] == "passed"
    assert rec["deletion"]["disposition"] == "dead_partial"
    assert rec["undo"]["op"] == "restore_from_trash"
    assert not partial.exists() and not lib.qbit.has(t.hash)
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident


def test_only_the_dead_torrents_own_partials_leave_the_season(lib):
    """NoSubfolder 死种与健康的邻居共用 Season 目录：邻居一个字节都不动。"""
    s1 = lib.show("尼古喵喵").season(1)
    healthy = s1.torrent({"尼古喵喵 S01E01.mkv": GB, "尼古喵喵 S01E02.mkv": GB},
                         name="[Group] Yani Neko 01-02", layout="nosub")
    dead = _dead(s1, {"尼古喵喵 S01E11.mkv": GB, "尼古喵喵 S01E12.mkv": GB},
                 "[TV版&无修版] 尼古喵喵 - EP11-12", layout="nosub")
    before = lib.disk()

    lib.converge()

    gone = {"尼古喵喵/Season 1/尼古喵喵 S01E11.mkv.!qB", "尼古喵喵/Season 1/尼古喵喵 S01E12.mkv.!qB"}
    assert set(before) - set(lib.disk()) == gone
    assert {k: v for k, v in before.items() if k not in gone} == lib.disk()
    assert len(lib.trash_files()) == 2
    assert lib.qbit.has(healthy.hash) and not lib.qbit.has(dead.hash)


def test_partial_that_another_torrent_claims_is_left_alone(lib):
    """另一个种子（换源抓来、已改到同一个集位名上的新种子）正往 `X` 写：那份 `.!qB`
    可能就是它接着写的，不动（I2）。死种的记录照摘。"""
    s1 = lib.show("尼古喵喵").season(1)
    dead = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    other = s1.torrent({"尼古喵喵 S01E11.mkv": GB}, name="[B] Yani Neko - 11.mkv",
                       layout="single", progress=0.0)
    partial = s1.path / "尼古喵喵 S01E11.mkv.!qB"

    c = lib.cycle(detectors=[DeadTorrentDetector])

    assert c.applied("drop_torrent") and not lib.qbit.has(dead.hash)
    [skip] = c.skipped("trash")
    assert skip["reason"].startswith("删除关口：I2") and other.hash[:8] in skip["reason"]
    assert partial.exists()


def test_partial_stays_when_the_drop_did_not_happen(lib):
    """诊断之后死种又有了做种：摘除被跳过，它还在下——它的半成品一个字节都不动。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    findings = lib.diagnose(detectors=[DeadTorrentDetector])
    assert sorted(f.action.op for f in findings) == ["drop_torrent", "trash"]
    lib.qbit.raw(t.hash)["num_complete"] = 3
    before = lib.snapshot()

    rep = lib.apply(findings)

    assert not rep.applied
    [skip] = [r for r in rep.skipped if r["op"] == "trash"]
    assert skip["reason"].startswith("删除关口：") and "还在" in skip["reason"]
    assert lib.snapshot() == before


def test_dead_pack_with_completed_members_leaves_its_partials(lib):
    """有已下完成员的死种只报告、不摘——半成品也不动。"""
    s1 = lib.show("银八").season(1)
    t = _dead(s1, {"银八 S01E01.mkv": GB, "银八 S01E02.mkv": GB}, "[G] Gintama 01-02",
              layout="nosub")
    lib.qbit.raw(t.hash)["_files"][0]["progress"] = 1

    c = lib.cycle(detectors=[DeadTorrentDetector])

    assert not c.actions()
    assert lib.trash_files() == []


def test_dead_partial_disposition_never_moves_a_finished_file(lib):
    """纵深防御：死种处置只许动 `.!qB`——哪怕参数被写成了正片的路径。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single("尼古喵喵 S01E11.mkv", size=GB)
    s1.local("尼古喵喵 S01E11 [other].mkv", size=GB)
    f = Finding(rule="dead-torrent", kind="dead_partial", severity="minor", summary="x",
                show="尼古喵喵", path=str(t.path), torrent_hash=t.hash,
                action=Action(op="trash", args={"path": str(t.path), "torrent_hash": t.hash}))
    ex = Executor(lib.context(), dry_run=False, run_id="u")
    ex._removed_torrents.add(t.hash)

    v = gate.check_trash(ex, f, t.path)

    assert v.refused and "半成品" in v.refused


def test_rollback_brings_the_partial_back_and_readds_the_torrent(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single")
    partial = s1.path / "尼古喵喵 S01E11.mkv.!qB"
    ident = lib.ident(partial)
    c = lib.cycle(detectors=[DeadTorrentDetector])

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 2, res
    assert lib.ident(partial) == ident and lib.qbit.has(t.hash)


def test_partial_record_remembers_the_torrent_that_was_dropped_before_it(lib):
    """purge 要的事实在删除那一刻最全：半成品进隔离区时它的种子已经在同一批里被摘了，
    `deletion.subject` 仍记着那个种子是谁（名字 / 钉子 / 标签 / 分类 / 文件数）。"""
    s1 = lib.show("尼古喵喵").season(1)
    t = _dead(s1, {"尼古喵喵 S01E11.mkv": GB}, "[A] Yani Neko - 11.mkv", layout="single",
              tags="ab:32, ma:S01E11")

    c = lib.cycle(detectors=[DeadTorrentDetector])

    [rec] = c.applied("trash")
    d = rec["deletion"]
    assert d["rule"] == "dead-torrent"
    assert d["subject"] == {"torrent_hash": t.hash, "name": "[A] Yani Neko - 11.mkv",
                            "pin": "S01E11", "tags": "ab:32, ma:S01E11",
                            "category": "尼古喵喵", "torrent_files": 1,
                            "removed_this_batch": True}
