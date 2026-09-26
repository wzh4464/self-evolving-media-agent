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
