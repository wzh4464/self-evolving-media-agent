"""路径占用原语 `media_agent.claims`：一个绝对路径此刻归谁。

critic N6：「这个路径是否已被另一个活种子声明」必须是所有写路径共用的一道闸。
以前每个写路径各查各的：`_op_rename` 只看盘上 `target.exists()`（第 9 条修复补了一个
只认完全相同字符串的 `_claimants`），抓取后的即时改名、relink、回退一概不查。
libtorrent 2.0.13 只在**源文件存在**且目标存在时才报 "File exists"；源文件还没落盘
（0%、metaDL 之后）或只有 `.!qB` 时，`renameFile` 只改映射——两个种子从此宣称同一个
路径，直到完成那一刻 `X.!qB → X` 撞上 EEXIST（生产 2026-08-29 穹庐下的魔女 S01E09：
`- 09` 下载中被改到 X，`- 09v2` 完成后也被改到 X，留下 789MB 的孤儿 `X.!qB`）。

这里只测原语本身；各写路径的接线在各自的测试文件里。
"""
from __future__ import annotations

import os
import unicodedata

import httpx
import pytest

from media_agent.claims import ClaimIndex, fold

GB = 600_000_000
SLOT = "尼古喵喵 S01E08.mkv"
RAW = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"


def _hashes(chk) -> list[str]:
    return sorted(c.hash for c in chk.claimants if c.kind == "qbit")


def _disk(chk) -> list[tuple[str, bool]]:
    return sorted((os.path.basename(c.path), c.partial)
                  for c in chk.claimants if c.kind == "disk")


# ------------------------------------------------------------------ 谁算占用
def test_two_torrents_claiming_one_name_are_both_reported(lib, fs):
    """2026-09-06 尼古喵喵 S01E08 的形态：两个种子的 `torrents/files` 报着同一个路径。"""
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.single(SLOT, size=GB)
    b = s1.torrent({SLOT: GB}, name="[B] Yani Neko - 08.mkv", layout="single", progress=0.0)
    idx = ClaimIndex(lib.qbit)

    anyone = idx.check(s1.path / SLOT)
    as_a = idx.check(s1.path / SLOT, own_hash=a.hash, own_path=s1.path / SLOT)

    assert _hashes(anyone) == sorted([a.hash, b.hash]) and _disk(anyone) == [(SLOT, False)]
    assert not anyone.free and not anyone.unknown
    # 问的人自己的条目、自己的文件不算；另一个种子照样算
    assert _hashes(as_a) == [b.hash] and _disk(as_a) == []


def test_partial_of_another_torrent_occupies_the_name(lib, fs):
    """盘上只有 `X.!qB`：别的种子正往这个名字里写。"""
    s1 = lib.show("尼古喵喵").season(1)
    other = s1.single(SLOT, size=GB, progress=0.5)            # 盘上是 SLOT.!qB
    me = s1.single(RAW, size=GB)
    assert (s1.path / (SLOT + ".!qB")).exists() and not (s1.path / SLOT).exists()

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT, own_hash=me.hash, own_path=me.path)

    assert _disk(chk) == [(SLOT + ".!qB", True)]
    assert _hashes(chk) == [other.hash]


def test_orphan_partial_still_occupies_after_its_torrent_is_gone(lib):
    """种子记录摘了、`.!qB` 留在盘上（死种处置就是这样）：改名过去会接着往这份
    半成品里写，照样算占用。"""
    s1 = lib.show("尼古喵喵").season(1)
    other = s1.single(SLOT, size=GB, progress=0.4)
    lib.qbit.delete([other.hash], delete_files=False)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert _disk(chk) == [(SLOT + ".!qB", True)] and _hashes(chk) == []


def test_zero_percent_torrent_already_mapped_to_the_name(lib):
    """0% 的种子盘上什么都没有，但 `torrents/files` 已经把它映射到这个名字。"""
    s1 = lib.show("尼古喵喵").season(1)
    zero = s1.single(SLOT, size=GB, progress=0.0)
    assert not any(s1.path.iterdir())

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert _hashes(chk) == [zero.hash] and _disk(chk) == []


def test_torrent_in_a_subfolder_layout_is_matched_by_its_full_path(lib):
    """Original 布局：save_path 在剧目录、条目是 `Season 1/…`——按 save_path + 条目名比，
    不是按文件名。"""
    sh = lib.show("尼古喵喵")
    sh.season(1)
    t = sh.folder("").torrent({SLOT: GB}, name="Season 1", layout="original", progress=0.0)
    elsewhere = sh.folder("Specials").torrent({SLOT: GB}, name="x", layout="nosub",
                                              progress=0.0)

    chk = ClaimIndex(lib.qbit).check(sh.path / "Season 1" / SLOT)

    assert _hashes(chk) == [t.hash]
    assert elsewhere.hash not in _hashes(chk)


def test_priority_zero_entries_and_dropped_torrents_do_not_claim(lib):
    s1 = lib.show("尼古喵喵").season(1)
    s1.torrent({SLOT: GB, "b.mkv": GB}, name="[G] pack", layout="nosub", progress=0.0,
               priorities={SLOT: 0})
    gone = s1.single(SLOT, size=GB, progress=0.0, name="[D] dead.mkv")
    removed: set[str] = set()
    idx = ClaimIndex(lib.qbit, ignore=removed)

    assert _hashes(idx.check(s1.path / SLOT)) == [gone.hash]
    removed.add(gone.hash)                       # 执行器本批次摘掉了它（引用，不是拷贝）
    assert idx.check(s1.path / SLOT).free


# ------------------------------------------------------------------ 大小写 / Unicode 规范化
def test_case_only_difference_is_the_same_name_in_qbit(lib):
    """生产媒体卷是大小写不敏感的 APFS：`S01E08.mkv` 与 `s01e08.MKV` 是同一个文件。"""
    s1 = lib.show("尼古喵喵").season(1)
    other = s1.single("尼古喵喵 s01e08.MKV", size=GB, progress=0.0)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert _hashes(chk) == [other.hash]


def test_case_only_difference_is_the_same_name_on_disk_on_any_filesystem(lib, fs):
    """盘上的比较不能依赖宿主文件系统：CI 的 Linux 区分大小写，照样要认出来。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    s1.local("尼古喵喵 s01e08.MKV", size=GB)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert [c.kind for c in chk.claimants] == ["disk"]
    assert fold(os.path.basename(chk.claimants[0].path)) == fold(SLOT)


def test_unicode_normalization_form_is_the_same_name(lib, fs):
    """APFS 同样不区分 NFC / NFD：`ガ`（U+30AC）与 `カ` + 浊点（U+30AB U+3099）。"""
    s1 = lib.show("ガヴリール").season(1)
    nfc = "ガヴリール S01E01.mkv"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    t = s1.single(nfd, size=GB, progress=0.0)
    loose = lib.show("ガヴリール").folder("Season 2").local(nfd, size=GB)

    q = ClaimIndex(lib.qbit).check(s1.path / nfc)
    d = ClaimIndex(lib.qbit).check(loose.parent / nfc)

    assert _hashes(q) == [t.hash]
    assert [c.kind for c in d.claimants] == ["disk"]


def test_case_only_self_rename_of_own_file_is_free(lib, fs):
    """自己的文件只改大小写：盘上"已存在"的正是它自己，种子里声明它的也是自己。"""
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single("尼古喵喵 s01e08.mkv", size=GB)
    loose = s1.local("尼古喵喵 s01e09.MKV", size=GB)
    idx = ClaimIndex(lib.qbit)

    assert idx.check(s1.path / SLOT, own_hash=me.hash, own_path=me.path).free
    assert idx.check(s1.path / "尼古喵喵 S01E09.mkv", own_path=loose).free


def test_case_only_self_rename_still_sees_a_second_claimant(lib):
    """自己改大小写不被自己挡住，但同一路径（忽略大小写）若还有别的种子声明，照样算占用。"""
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single("尼古喵喵 s01e08.mkv", size=GB)
    other = s1.torrent({"尼古喵喵 S01E08.MKV": GB}, name="[O] x.mkv", layout="single",
                       progress=0.0)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT, own_hash=me.hash, own_path=me.path)

    assert _hashes(chk) == [other.hash] and _disk(chk) == []


def test_own_partial_is_exempt_when_only_the_case_changes(lib, fs):
    s1 = lib.show("尼古喵喵").season(1)
    me = s1.single("尼古喵喵 s01e08.mkv", size=GB, progress=0.3)   # 盘上是 …mkv.!qB

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT, own_hash=me.hash, own_path=me.path)

    assert chk.free, chk.describe()


def test_disk_side_can_be_skipped_for_relink(lib):
    """relink 的目标文件本来就在盘上——它要问的只是"有没有别的活种子声明它"。"""
    lib.configure(qbit_allow_empty=True)
    s1 = lib.show("尼古喵喵").season(1)
    f = s1.local(SLOT, size=GB)

    assert not ClaimIndex(lib.qbit).check(f).free
    assert ClaimIndex(lib.qbit).check(f, disk=False).free


# ------------------------------------------------------------------ fail closed + 缓存
def test_qbit_unavailable_is_unknown_not_free(lib):
    s1 = lib.show("尼古喵喵").season(1)

    chk = ClaimIndex(None).check(s1.path / SLOT)

    assert chk.unknown and not chk.free


def test_torrent_list_failure_is_unknown(lib):
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(RAW, size=GB)
    lib.qbit.fail("torrents", exc=httpx.ReadTimeout("timed out (injected)"))

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert chk.unknown and "torrents()" in chk.unknown and not chk.free


def test_file_list_failure_of_a_relevant_torrent_is_unknown(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(RAW, size=GB)
    lib.qbit.fail("files", hash=t.hash)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert chk.unknown and t.hash[:8] in chk.unknown and not chk.free


def test_file_list_failure_of_an_unrelated_torrent_does_not_block(lib):
    """只问 save_path 是目标祖先的种子：别处的种子读不到，不影响这个判断。"""
    s1 = lib.show("尼古喵喵").season(1)
    far = lib.show("别的番").season(1).single(RAW, size=GB)
    lib.qbit.fail("files", hash=far.hash, times=None)

    assert ClaimIndex(lib.qbit).check(s1.path / SLOT).free


@pytest.mark.parametrize("call", ["scandir", "lstat"])
def test_an_unreadable_directory_is_unknown_not_free(lib, monkeypatch, call):
    """2026-09-26 审查：盘上那一侧读不了（权限、I/O 错误）时同样不知道——以前把 `ClaimsUnknown`
    换成"当它不在"全套照样全绿（C18 / C19）。看不见的名字不等于没人占。"""
    from media_agent import claims as claims_mod

    s1 = lib.show("尼古喵喵").season(1)
    s1.local(SLOT, size=GB)
    real = getattr(os, call)

    def denied(p, *a, **k):
        if str(s1.path) in str(p):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(claims_mod.os, call, denied)

    chk = ClaimIndex(lib.qbit).check(s1.path / SLOT)

    assert chk.unknown and not chk.free and "Permission" in chk.unknown


@pytest.mark.allow("qbit_error", match="404")
def test_torrent_deleted_between_list_and_files_claims_nothing(lib):
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(SLOT, size=GB, progress=0.0)
    idx = ClaimIndex(lib.qbit)
    idx.torrents()                                   # 列表里还有它
    lib.qbit.delete([t.hash], delete_files=False)    # 读文件列表之前被删了

    chk = idx.check(s1.path / SLOT)

    assert chk.free and not chk.unknown


def test_index_is_built_once_until_invalidated(lib):
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(RAW, size=GB)
    idx = ClaimIndex(lib.qbit)

    def reads():
        return sum(1 for c in lib.qbit.calls if c[0] in ("torrents", "files"))

    idx.check(s1.path / SLOT)
    first = reads()
    idx.check(s1.path / "尼古喵喵 S01E09.mkv")
    assert reads() == first                          # 第二次全走缓存
    idx.invalidate()
    idx.check(s1.path / SLOT)
    assert reads() == 2 * first                      # 作废后重新问 qBittorrent


def test_stale_index_misses_a_claim_made_after_it_was_built(lib):
    """缓存的代价：建索引之后才出现的声明看不见——所以执行器每做一次改动就作废它。"""
    s1 = lib.show("尼古喵喵").season(1)
    idx = ClaimIndex(lib.qbit)
    assert idx.check(s1.path / SLOT).free
    late = s1.single(SLOT, size=GB, progress=0.0)

    assert idx.check(s1.path / SLOT).free            # 旧索引
    idx.invalidate()
    assert _hashes(idx.check(s1.path / SLOT)) == [late.hash]


# ------------------------------------------------------------------ 目录
def test_directory_check_sees_disk_and_torrents_inside_it(lib):
    sh = lib.show("新名")
    inside = sh.season(1).single(SLOT, size=GB, progress=0.0)
    root_level = lib.show("根").folder("").torrent({"新名/Season 1/x.mkv": GB}, name="x",
                                                  layout="nosub", progress=0.0)
    lib.qbit.raw(root_level.hash)["save_path"] = str(lib.media_root)
    idx = ClaimIndex(lib.qbit)

    chk = idx.check_dir(sh.path)
    movers = idx.check_dir(sh.path, movers=[inside.hash, root_level.hash])

    assert [c.kind for c in chk.claimants].count("disk") == 1
    assert _hashes(chk) == sorted([inside.hash, root_level.hash])
    assert _hashes(movers) == []


def test_directory_check_on_an_absent_dir_with_a_zero_percent_torrent(lib):
    """目录盘上还不存在，但已有种子把 save_path 指到了它下面（metaDL 也算）。"""
    t = lib.show("别处").season(1).single(SLOT, size=GB, progress=0.0)
    target = lib.media_root / "新名"
    lib.qbit.raw(t.hash)["save_path"] = str(target / "Season 1")
    lib.qbit.raw(t.hash)["_files"] = []              # 还没有元数据

    chk = ClaimIndex(lib.qbit).check_dir(target)

    assert _hashes(chk) == [t.hash] and not [c for c in chk.claimants if c.kind == "disk"]


def test_directory_check_is_case_insensitive(lib, fs):
    lib.configure(qbit_allow_empty=True)
    lib.show("Gnosia").season(1)

    chk = ClaimIndex(lib.qbit).check_dir(lib.media_root / "GNOSIA")

    assert [c.kind for c in chk.claimants] == ["disk"]


# ------------------------------------------------------------------ 目录之下谁有文件
def test_claims_under_lists_owners_and_every_claimed_path_folded(lib):
    """目录级搬运的"不许文件系统碰"清单：优先级 0 的条目也算（setLocation 会连它一起搬），
    save_path 在更上层、根文件夹就叫剧名的 Original 种子也算，`.!qB` 形态也在里面。"""
    sh = lib.show("剧名")
    inside = sh.season(1).torrent({"a.mkv": GB, "b.mkv": GB}, name="[G] pack", layout="nosub",
                                  priorities={"b.mkv": 0})
    top = lib.show("_").folder("").torrent({"Season 2/c.mkv": GB}, name="剧名",
                                           layout="original", progress=0.0)
    lib.qbit.raw(top.hash)["save_path"] = str(lib.media_root)
    far = lib.show("别的番").season(1).single("x.mkv", size=GB)

    owners, claimed = ClaimIndex(lib.qbit).claims_under(sh.path)

    assert sorted(owners) == sorted([inside.hash, top.hash]) and far.hash not in owners
    s1 = sh.path / "Season 1"
    for p in (s1 / "a.mkv", s1 / "b.mkv", s1 / "b.mkv.!qB", sh.path / "Season 2" / "c.mkv"):
        assert fold(p) in claimed and claimed[fold(p)] == p
    assert fold(str(s1 / "A.MKV")) in claimed                   # 只差大小写也认得出


def test_claims_under_is_unknown_when_a_relevant_file_list_cannot_be_read(lib):
    from media_agent.claims import ClaimsUnknown
    sh = lib.show("剧名")
    t = sh.season(1).single("a.mkv", size=GB)
    lib.qbit.fail("files", hash=t.hash)

    with pytest.raises(ClaimsUnknown):
        ClaimIndex(lib.qbit).claims_under(sh.path)
