"""duplicate-episode 喂给删除关口的东西：输家的动作参数、封存的选择。

- **D1**：判重输家以前只给 `{path, torrent_hash}`——执行器因此整种子作废（多文件合集的一集输了，
  其余集跟着失去做种；生产审计 63 条，例：3年Z组银八老师 [01-12]），关口也无从复核保留方。
  现在输家一律 `file_only`（执行器按种子此刻要下载的文件数决定是否整种子作废），并带上
  保留方（路径 / 种子 / 诊断时大小 / 内容摘要）与解析出的集位。
"""
from __future__ import annotations

from harness import video
from media_agent.plugins.builtin import DuplicateEpisodeDetector

GB = 600_000_000
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")
TITLE = "3年Z组银八老师"


def _gintama(lib):
    s1 = lib.show(TITLE).season(1)
    files = {f"[G] Ginpachi-sensei - {n:02d} [1080p].mkv": GB for n in range(1, 13)}
    pack = s1.torrent(files, name="[G] 3-nen Z-gumi Ginpachi-sensei [01-12]", layout="nosub",
                      probe=RAW)
    better = s1.single("[LoliHouse] Ginpachi-sensei - 05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
                       size=GB - 7, probe=CHI)
    return s1, pack, better


def test_loser_action_names_its_keeper_and_slot_and_is_file_only(lib):
    s1, pack, better = _gintama(lib)

    [dup] = [f for f in lib.diagnose(detectors=[DuplicateEpisodeDetector])
             if f.kind == "duplicate"]

    args = dup.action.args
    assert args["path"] == str(s1.path / "[G] Ginpachi-sensei - 05 [1080p].mkv")
    assert args["torrent_hash"] == pack.hash
    assert args["file_only"] is True
    assert args["slot"] == [1, 5]
    assert args["keep_path"] == str(better.path)
    assert args["keep_hash"] == better.hash
    assert args["keep_size"] == GB - 7
    assert args["keep_digest"] == dup.evidence["keep_digest"]


def test_pack_member_that_loses_only_leaves_the_pack(lib):
    """端到端：合集照常做种，只有输掉的那一集被设为不下载并移进隔离区；
    审计里记着保留方与集位（purge 以后据此判断能不能真删）。"""
    s1, pack, better = _gintama(lib)
    loser = s1.path / "[G] Ginpachi-sensei - 05 [1080p].mkv"
    ident = lib.ident(loser)

    c = lib.cycle()

    [rec] = c.applied("trash")
    assert rec["args"]["path"] == str(loser)
    assert rec["deletion"]["gate"] == "passed"
    assert rec["deletion"]["keeper"]["hash"] == better.hash
    assert rec["deletion"]["keeper"]["digest"] == rec["args"]["keep_digest"]
    assert rec["deletion"]["slot"] == [1, 5]
    assert rec["undo"]["torrent_record_lost"] is False
    assert lib.qbit.has(pack.hash)
    pri = {f["name"]: f["priority"] for f in lib.qbit.raw(pack.hash)["_files"]}
    assert pri.pop("[G] Ginpachi-sensei - 05 [1080p].mkv") == 0
    assert set(pri.values()) == {1}
    [moved] = lib.trash_files()
    assert lib.ident(moved) == ident
    assert (s1.path / f"{TITLE} S01E05.mkv").exists()          # 赢家同批拿到集位名


def test_bundled_sibling_names_the_keeper_in_the_same_torrent(lib):
    s1 = lib.show("尼古喵喵").season(1)
    bundle = s1.torrent({"【7月】尼古喵喵 11【TV版】.mp4": GB,
                         "【7月】尼古喵喵 11【邪龙解放版】.mp4": GB},
                        name="[TV版&无修版] 尼古喵喵 - EP11 [简／繁] (1080p H.264 AAC SRTx2)",
                        layout="nosub", tags="ma:S01E11", probe=CHI)
    tv, xie = bundle.paths

    [sib] = [f for f in lib.diagnose(detectors=[DuplicateEpisodeDetector])
             if f.kind == "bundled_version"]

    assert sib.action.args["path"] == str(tv)
    assert sib.action.args["keep_path"] == str(xie)
    assert sib.action.args["keep_hash"] == bundle.hash
    assert sib.action.args["slot"] == [1, 11]


# ------------------------------------------------------------------ D5：两个种子都封存着同一集
LOLI = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
NEST = "[NEST] Yani Neko - 08 [NF WEB-DL 1080p AVC AAC][简繁日内封].mkv"


def _two_seals(lib):
    s1 = lib.show("尼古喵喵").season(1)
    a = s1.single(LOLI, size=GB, tags="ma:S01E08", probe=CHI)
    b = s1.single(NEST, size=GB + 5, tags="ma:S01E08", probe=CHI)
    return s1, a, b


def _trash_paths(findings):
    return {f.action.args["path"] for f in findings if f.action and f.action.op == "trash"}


def test_two_torrents_sealing_one_slot_are_both_kept_and_reported(lib):
    """停滞 48 小时放行换源、或手动加了同钉子的种子之后：两个不同的种子都钉着 S01E08、
    都复核通过。以前只封存偏好分最高的那个、另一个当输家删掉——两个都是择源的结论，
    删哪个该由人定。"""
    s1, a, b = _two_seals(lib)

    found = lib.diagnose(detectors=[DuplicateEpisodeDetector])

    assert _trash_paths(found) == set()
    [conflict] = [f for f in found if f.kind == "seal_conflict"]
    assert conflict.action is None and conflict.classified
    assert sorted(conflict.evidence["torrents"]) == sorted([a.hash, b.hash])


def test_seal_conflict_still_clears_an_unsealed_third_copy(lib):
    """第三份没钉 `ma:` 的照常判输——保留方是偏好分最高的那份封存。"""
    s1, a, b = _two_seals(lib)
    raw = s1.single("[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv",
                    size=GB + 9, probe=RAW)

    found = lib.diagnose(detectors=[DuplicateEpisodeDetector])

    assert _trash_paths(found) == {str(raw.path)}
    [dup] = [f for f in found if f.kind == "duplicate"]
    assert dup.action.args["keep_hash"] in (a.hash, b.hash)
    assert [f.kind for f in found if f.kind == "seal_conflict"] == ["seal_conflict"]


def test_seal_conflict_cycle_trashes_nothing_sealed(lib):
    s1, a, b = _two_seals(lib)
    ia, ib = lib.ident(a.path), lib.ident(b.path)

    lib.converge()

    assert lib.trash_files() == []
    assert lib.qbit.has(a.hash) and lib.qbit.has(b.hash)
    assert {lib.ident(p) for p in s1.path.iterdir()} == {ia, ib}


# ------------------------------------------------------------------ 封存要稳定
def test_pinned_copy_whose_probe_is_unavailable_is_not_made_a_loser(lib):
    """钉着 `ma:S01E08` 的 LoliHouse 版，这一轮 ffprobe 超时（探测返回 None）：只看名字，
    `ASSx2` 过不了硬门槛。以前它就此失封，排名又输给探得到双字幕轨的另一份，被当输家删掉。
    现在"不知道"当作封存：它不当输家，也不当赢家，报一条给人看。"""
    s1 = lib.show("尼古喵喵").season(1)
    pinned = s1.single(LOLI, size=GB, tags="ma:S01E08", probe=None)
    s1.single("[ANi] Yani Neko - 08 [1080P][Baha][WEB-DL][AAC AVC][CHT].mkv", size=GB + 5,
              probe=CHI)

    found = lib.diagnose(detectors=[DuplicateEpisodeDetector])

    assert str(pinned.path) not in _trash_paths(found)
    [unk] = [f for f in found if f.kind == "seal_unknown"]
    assert unk.path == str(pinned.path) and unk.action is None
