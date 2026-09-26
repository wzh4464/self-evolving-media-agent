"""回退里往媒体库写路径的三个逆操作也走占用闸门（critic N6）。

- **逆改名**以前只看 `back.exists()`：原名此后被一个 0% 的新种子映射了（盘上什么都没有），
  照改——两个种子声明同一路径；反过来，自己当初只改了大小写，APFS 上 `back.exists()`
  为真（就是它自己），回退永远跳过。
- **重加种子**（`readd_torrent`，死种 / 撞车 / 幻影摘记录的逆操作）以前什么都不查：
  被摘的那个种子声明过的路径此后若被别的种子占了，凭 magnet 加回来就又是两个种子
  争一个文件。例外是撞车受害者：当初保留的那一方（`keep_hash`）本来就在，回退就是
  要恢复"暂停着、等人看"的原状。
- **从隔离区搬回**以前只看盘上：原位置此后被一个还没落盘的下载映射了，搬回去之后
  那个下载完成时 `X.!qB → X` 撞 EEXIST。
"""
from __future__ import annotations

import json

import httpx
import pytest

from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import CollidingTorrentDetector, DeadTorrentDetector

GB = 600_000_000
SLOT = "尼古喵喵 S01E09.mkv"
RAW = "[LoliHouse] Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"


def _rename(path, new_name, h="") -> Finding:
    return Finding(rule="unrenamed-file", kind="unrenamed", severity="important",
                   summary=f"{path.name} → {new_name}", show="尼古喵喵", path=str(path),
                   torrent_hash=h,
                   action=Action(op="rename", args={"path": str(path), "new_name": new_name,
                                                    "torrent_hash": h}))


def _dead(d, files, name, **kw):
    kw.setdefault("progress", 0.4)
    kw.setdefault("state", "stalledDL")
    kw.setdefault("added_hours_ago", 24 * 30)
    kw.setdefault("availability", 0)
    kw.setdefault("num_complete", 0)
    return d.torrent(files, name=name, **kw)


def _claims_of(lib, path) -> list[str]:
    return sorted(h for h in lib.qbit.snapshot()
                  if path in [lib.path(lib.rel(lib.qbit.raw(h)["save_path"])) / n
                              for n in lib.qbit.file_names(h)])


# ------------------------------------------------------------------ 逆改名
def test_undo_rename_refuses_a_name_a_new_torrent_now_claims(lib):
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single(RAW, size=GB)
    lib.apply([_rename(me.path, SLOT, me.hash)], run_id="fw")
    newcomer = s1.torrent({RAW: GB}, name="[X] same release, re-grabbed", layout="single",
                          progress=0.0)
    before = lib.snapshot()

    res = lib.rollback("fw")

    assert res["reverted"] == 0 and res["skipped"] == 1 and res["failed"] == 0
    assert newcomer.hash[:8] in res["skipped_detail"][0]["skip_reason"]
    assert lib.snapshot() == before
    assert _claims_of(lib, s1.path / RAW) == [newcomer.hash]


def test_undo_of_a_case_only_rename_is_not_blocked_by_itself(lib):
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single("尼古喵喵 s01e09.mkv", size=GB)
    lib.apply([_rename(me.path, SLOT, me.hash)], run_id="fw")
    assert lib.qbit.file_names(me.hash) == [SLOT]

    res = lib.rollback("fw")

    assert res["reverted"] == 1, res["skipped_detail"]
    assert lib.qbit.file_names(me.hash) == ["尼古喵喵 s01e09.mkv"]


def test_undo_rename_is_refused_when_occupancy_cannot_be_read(lib):
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single(RAW, size=GB)
    lib.apply([_rename(me.path, SLOT, me.hash)], run_id="fw")
    lib.qbit.fail("torrents", exc=httpx.ReadTimeout("timed out (injected)"))
    before = lib.snapshot()

    res = lib.rollback("fw")

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "无法确认" in res["skipped_detail"][0]["skip_reason"]
    assert lib.snapshot() == before


# 2026-09-26 审查：逆改名"原名盘上已被别的文件占着"那一支（`chk.claimants` 的盘上一侧）没有测试，
# 纯本地文件的 `cur.rename(back)` 一次都没跑过——关掉这道检查全套照样全绿，而 os.rename 会无声覆盖。
@pytest.mark.parametrize("newcomer_name", ["[G] Yani Neko - 05.mkv", "[g] yani neko - 05.MKV"],
                         ids=["same-name", "case-only"])
def test_undo_rename_of_a_local_file_never_overwrites_a_file_at_the_old_name(lib, fs,
                                                                           newcomer_name):
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.local("[G] Yani Neko - 05.mkv", size=GB)
    ident_a = lib.ident(a)
    rep = lib.apply([_rename(a, "尼古喵喵 S01E05.mkv")], run_id="fw")
    assert len(rep.applied) == 1 and not a.exists()
    newcomer = s1.local(newcomer_name, size=GB // 2)       # 此后有人在原名上放了另一个文件
    ident_n = lib.ident(newcomer)

    res = lib.rollback("fw")

    assert res["reverted"] == 0 and res["skipped"] == 1, res
    assert "已存在" in res["skipped_detail"][0]["skip_reason"]
    assert lib.ident(newcomer) == ident_n
    assert lib.ident(s1.path / "尼古喵喵 S01E05.mkv") == ident_a


def test_undo_rename_of_a_local_file_goes_back_when_the_old_name_is_free(lib):
    """对照：原名空着，纯本地文件按文件系统改回去。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.local("[G] Yani Neko - 05.mkv", size=GB)
    ident_a = lib.ident(a)
    lib.apply([_rename(a, "尼古喵喵 S01E05.mkv")], run_id="fw")

    res = lib.rollback("fw")

    assert res["reverted"] == 1, res
    assert lib.ident(s1.path / "[G] Yani Neko - 05.mkv") == ident_a
    assert not (s1.path / "尼古喵喵 S01E05.mkv").exists()


# ------------------------------------------------------------------ 重加种子
def test_readd_of_a_dead_torrent_is_refused_when_its_path_is_now_claimed(lib):
    """死种被摘；之后换源抓来的新种子占了同一个名字。回退加回死种 = 两个种子争一个文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    dead = _dead(s1, {SLOT: GB}, SLOT, layout="single")
    c = lib.cycle(detectors=[DeadTorrentDetector],
                  select=lambda f: f.action.op == "drop_torrent")   # 只看摘记录这一步
    [drop] = c.applied("drop_torrent")
    assert drop["undo"]["paths"] == [str(s1.path / SLOT)]      # 摘的时候记下它声明的路径
    newcomer = s1.torrent({SLOT: GB}, name="[LoliHouse] Yani Neko - 09.mkv", layout="single",
                          progress=0.0)
    before = lib.snapshot()

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    reason = res["skipped_detail"][0]["skip_reason"]
    assert newcomer.hash[:8] in reason and "magnet" in reason
    assert not lib.qbit.has(dead.hash)
    assert lib.snapshot() == before
    assert _claims_of(lib, s1.path / SLOT) == [newcomer.hash]


def test_readd_of_an_old_record_without_paths_falls_back_to_the_display_name(lib):
    """第 2 阶段之前写下的记录没有 `paths`：单文件种子的显示名就是它的文件名，照样查得到。"""
    s1 = lib.show("尼古喵喵").season(1)
    dead = _dead(s1, {SLOT: GB}, SLOT, layout="single")
    c = lib.cycle(detectors=[DeadTorrentDetector],
                  select=lambda f: f.action.op == "drop_torrent")   # 只看摘记录这一步
    lines = lib.cfg.audit_log.read_text(encoding="utf-8").splitlines()
    recs = [json.loads(x) for x in lines]
    for r in recs:
        (r.get("undo") or {}).pop("paths", None)
    lib.cfg.audit_log.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                                 encoding="utf-8")
    s1.torrent({SLOT: GB}, name="[LoliHouse] Yani Neko - 09.mkv", layout="single", progress=0.0)

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert not lib.qbit.has(dead.hash)


def test_readd_of_a_dead_torrent_still_works_when_nothing_claims_its_path(lib):
    s1 = lib.show("尼古喵喵").season(1)
    dead = _dead(s1, {SLOT: GB}, SLOT, layout="single")
    c = lib.cycle(detectors=[DeadTorrentDetector],
                  select=lambda f: f.action.op == "drop_torrent")   # 只看摘记录这一步

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1
    assert lib.qbit.has(dead.hash)


def test_readd_of_a_colliding_victim_ignores_the_keeper_it_collided_with(lib):
    """撞车受害者被摘时，保留的那一方就声明着同一个文件——那是当初的原状，
    回退就是要把它暂停着加回来、等人看（`keep_hash` 不算占用者）。"""
    s1 = lib.show("尼古喵喵").season(1)
    keeper = s1.single(SLOT, size=GB, name="[K] Yani Neko - 09v2.mkv")
    victim = s1.torrent({SLOT: GB + 3}, name="[V] Yani Neko - 09.mkv", layout="single",
                        progress=0.97, state="stalledDL")
    c = lib.cycle(detectors=[CollidingTorrentDetector])
    [drop] = c.applied("drop_torrent")
    assert drop["args"]["keep_hash"] == keeper.hash

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1, res["skipped_detail"]
    assert lib.qbit.has(victim.hash)
    assert lib.qbit.torrent(victim.hash)["state"].startswith("stopped")   # 暂停着


def test_readd_of_a_colliding_victim_is_refused_when_a_third_torrent_claims_the_path(lib):
    s1 = lib.show("尼古喵喵").season(1)
    keeper = s1.single(SLOT, size=GB, name="[K] Yani Neko - 09v2.mkv")
    victim = s1.torrent({SLOT: GB + 3}, name="[V] Yani Neko - 09.mkv", layout="single",
                        progress=0.97, state="stalledDL")
    c = lib.cycle(detectors=[CollidingTorrentDetector])
    lib.qbit.delete([keeper.hash], delete_files=False)
    third = s1.torrent({SLOT: GB}, name="[T] Yani Neko - 09.mkv", layout="single",
                       progress=0.0)

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 0 and third.hash[:8] in res["skipped_detail"][0]["skip_reason"]
    assert not lib.qbit.has(victim.hash)


# ------------------------------------------------------------------ 从隔离区搬回
def _trash_record(lib, run_id: str, dst, src) -> None:
    rec = {"ts": "2026-09-20T17:01:26", "run_id": run_id, "status": "applied",
           "dry_run": False, "rule": "duplicate-episode", "kind": "duplicate", "op": "trash",
           "args": {"path": str(dst), "torrent_hash": ""}, "summary": "合成记录",
           "trashed_to": str(src), "freed_bytes": 7,
           "undo": {"op": "restore_from_trash", "path": str(dst), "trash_path": str(src),
                    "torrent_record_lost": False}}
    with lib.cfg.audit_log.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_restore_from_trash_refuses_a_path_a_download_now_claims(lib):
    """原位置盘上是空的，但一个 0% 的新种子已经映射到它。"""
    s1 = lib.show("尼古喵喵").season(1)
    src = lib.cfg.trash_dir / "2026-09-20" / "尼古喵喵" / "Season 1" / SLOT
    src.parent.mkdir(parents=True)
    src.write_bytes(b"trashed")
    _trash_record(lib, "tr", s1.path / SLOT, src)
    newcomer = s1.torrent({SLOT: GB}, name="[N] Yani Neko - 09.mkv", layout="single",
                          progress=0.0)

    res = lib.rollback("tr")

    assert res["reverted"] == 0 and res["skipped"] == 1
    reason = res["skipped_detail"][0]["skip_reason"]
    assert "占用" in reason and newcomer.hash[:8] in reason
    assert src.read_bytes() == b"trashed" and not (s1.path / SLOT).exists()


def test_restore_from_trash_refuses_a_case_only_variant_on_disk(lib, fs):
    """盘上已有只差大小写的同名文件：在大小写不敏感的卷上就是同一个路径（CI 的 Linux 上也要认出来）。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    s1.local("尼古喵喵 s01e09.MKV", size=GB)
    src = lib.cfg.trash_dir / "2026-09-20" / "尼古喵喵" / "Season 1" / SLOT
    src.parent.mkdir(parents=True)
    src.write_bytes(b"trashed")
    _trash_record(lib, "tr", s1.path / SLOT, src)

    res = lib.rollback("tr")

    assert res["reverted"] == 0 and "占用" in res["skipped_detail"][0]["skip_reason"]
    assert src.read_bytes() == b"trashed"


@pytest.mark.parametrize("fail", ["torrents", "files"])
def test_restore_from_trash_is_refused_when_occupancy_cannot_be_read(lib, fail):
    s1 = lib.show("尼古喵喵").season(1)
    neighbour = s1.single("尼古喵喵 S01E08.mkv", size=GB)
    src = lib.cfg.trash_dir / "2026-09-20" / "尼古喵喵" / "Season 1" / SLOT
    src.parent.mkdir(parents=True)
    src.write_bytes(b"trashed")
    _trash_record(lib, "tr", s1.path / SLOT, src)
    lib.qbit.fail(fail, hash=neighbour.hash if fail == "files" else None, times=None,
                  exc=httpx.ReadTimeout("timed out (injected)"))

    res = lib.rollback("tr")

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert "无法确认" in res["skipped_detail"][0]["skip_reason"]
    assert src.read_bytes() == b"trashed"


# ------------------------------------------------------------------ 恢复优先级
def _phantom_member(path, h) -> Finding:
    return Finding(rule="duplicate-episode", kind="bundled_version", severity="important",
                   summary="x", show="尼古喵喵", path=str(path), torrent_hash=h,
                   action=Action(op="trash", args={"path": str(path), "torrent_hash": h,
                                                   "phantom": True, "file_only": True}))


def test_restore_priority_is_refused_when_another_torrent_now_claims_the_entry(lib):
    """合集里的幻影条目被设为不下载；之后另一个种子映射到了同一个名字。把优先级恢复
    回来，qBittorrent 就又要往那个路径写——两个种子争一个文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    pack = s1.torrent({"尼古喵喵 S01E11.mkv": GB, SLOT: GB}, name="[G] 11-12", layout="nosub")
    _, e09 = pack.paths
    e09.unlink()                                          # 用户删了这一集：幻影
    lib.apply([_phantom_member(e09, pack.hash)], run_id="pz")
    assert [f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]] == [1, 0]
    newcomer = s1.torrent({SLOT: GB}, name="[N] Yani Neko - 09.mkv", layout="single",
                          progress=0.0)

    res = lib.rollback("pz")

    assert res["reverted"] == 0 and res["skipped"] == 1
    assert newcomer.hash[:8] in res["skipped_detail"][0]["skip_reason"]
    assert [f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]] == [1, 0]


# ------------------------------------------------------------------ 合集里被判输的一集：回退连优先级一起恢复
def _ginpachi(lib):
    """3年Z组银八老师：720p 生肉合集里的第 2 集输给 1080p 带中文字幕的单集（D1 + I3：只作废这一个条目）。"""
    from harness import video
    from media_agent.plugins.builtin import DuplicateEpisodeDetector

    s1 = lib.show("3年Z组银八老师").season(1)
    pack = s1.torrent({f"[G] Ginpachi-sensei - {n:02d} [720p].mkv": 600_000_000 for n in (1, 2, 3)},
                      name="[G] Ginpachi-sensei [01-03][720p]", layout="nosub",
                      probe=video("h264", height=720))
    s1.single("[H] Ginpachi-sensei - 02 [1080p].mkv", size=900_000_000,
              probe=video("hevc", subs=["chi 简体中文"]))
    c = lib.cycle(detectors=[DuplicateEpisodeDetector])
    [rec] = c.applied("trash")
    assert rec["undo"]["file_priority"]["priority"] == 1
    return s1, pack, c


def _pri(lib, h):
    return [f["priority"] for f in lib.qbit.raw(h)["_files"]]


def test_rollback_of_a_pack_member_trash_also_restores_its_download(lib):
    """2026-09-26 审查（复现）：D1 让每个判重输家都 `file_only`，I3 把合集成员降级成只把那一集设为不下载；
    逆操作只有 `restore_from_trash`，回退把文件搬回来、报「已还原 1」，条目却一直是优先级 0——
    搬回来的是一个没有种子做种的"本地文件"，下一轮 scan 就这么当它。"""
    s1, pack, c = _ginpachi(lib)
    assert _pri(lib, pack.hash) == [1, 0, 1]

    res = lib.rollback(c.run_id)

    assert res["reverted"] == 1 and res["priority_not_restored"] == 0, res
    assert _pri(lib, pack.hash) == [1, 1, 1]
    assert (s1.path / "[G] Ginpachi-sensei - 02 [720p].mkv").exists()


@pytest.mark.allow("qbit_error", match="404")
def test_rollback_says_so_when_the_file_came_back_but_the_download_did_not(lib, monkeypatch,
                                                                            capsys):
    """合集此后被人从 qBittorrent 里删了：文件照样搬回（它是主体），但报告里写明条目没恢复，不假装干净。"""
    import argparse

    from media_agent import cli

    s1, pack, c = _ginpachi(lib)
    lib.qbit.delete([pack.hash], delete_files=False)
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())

    rc = cli.cmd_rollback(argparse.Namespace(run=c.run_id, last=False, dry_run=False), lib.cfg)

    out = capsys.readouterr().out
    assert rc == 0 and "已还原: 1" in out
    assert "合集条目的下载没恢复: 1" in out and "不在" in out
    assert (s1.path / "[G] Ginpachi-sensei - 02 [720p].mkv").exists()
