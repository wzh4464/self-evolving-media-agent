"""复核与择优也看出处账本里的番组页标题（`builtin.release_text`）。

版本词常常只在 Mikan 站点的发布标题里，种子内部名没有：
- LoliHouse 的内部名只写 `[WebRip 1080p HEVC-10bit AAC ASSx2]`，「简繁内封字幕」只在番组页标题里——探测不可用
  （ffprobe 超时、出错）时，复核（`meets_requirements`）只能按内部名判它"没有中文字幕"：2026-09-18 初版就这样在
  36 个钉子里误判了 12 个（`meets_requirements` 的注释）；
- 尼古喵喵的「邪竜解放版」、合并发布的「无修版」同样只在标题里，择优（`_prefer_score`）与判重排序
  （`_rank_for_keep`）按内部名给它们 0 分。

账本里有这个种子的番组页标题时，名字证据 = 番组页标题 + 内部名（显示名 / 文件名）；没有时与以前一样。
"""
from __future__ import annotations

from harness import video

from media_agent import ledger
from media_agent.plugins.builtin import (DuplicateEpisodeDetector, _prefer_score, _rank_for_keep,
                                         meets_requirements, release_text)

LOLI_MIKAN = "[LoliHouse] 尼古喵喵 / Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
LOLI_FILE = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
XIE_MIKAN = "[G] 尼古喵喵 / Yani Neko - 11 [邪竜解放版][1080p][简繁内封]"
H1, H2 = "d" * 40, "e" * 40


def _row(h, title, **kw) -> ledger.Row:
    return ledger.Row(infohash=h, source=ledger.AUTOBANGUMI, mikan_title=title, **kw)


def test_release_text_puts_the_mikan_title_in_front(make_file):
    f = make_file(LOLI_FILE, torrent_hash=H1)
    assert release_text(f) == LOLI_FILE
    f.ledger = _row(H1, LOLI_MIKAN)
    assert release_text(f).startswith(LOLI_MIKAN) and LOLI_FILE in release_text(f)
    f.ledger = _row(H1, "")
    assert release_text(f) == LOLI_FILE


def test_an_unprobed_lolihouse_release_passes_by_its_mikan_title(make_file):
    f = make_file(LOLI_FILE, torrent_hash=H1, probe=None)          # 探测不可用
    assert meets_requirements(f)[0] is False                       # 只凭内部名：没有中文字幕的字样

    f.ledger = _row(H1, LOLI_MIKAN)
    ok, why = meets_requirements(f)
    assert ok is True and "探测不可用" in why


def test_the_mikan_title_counts_in_the_preference_score(make_file):
    xie = make_file("尼古喵喵 11 A.mkv", "[G] Yani Neko - 11 [1080p].mkv", torrent_hash=H1)
    tv = make_file("尼古喵喵 11 B.mkv", "[H] Yani Neko - 11 [1080p][简体].mkv", torrent_hash=H2)
    assert _prefer_score(xie) < _prefer_score(tv)                   # 内部名里没有「邪竜解放版」

    xie.ledger = _row(H1, XIE_MIKAN)
    assert _prefer_score(xie) > _prefer_score(tv)


def test_the_mikan_title_counts_in_the_duplicate_rank(make_file):
    loli = make_file(LOLI_FILE, torrent_hash=H1, size=560_000_000, probe=None)
    cht = make_file("[X] Yani Neko - 08 [1080p][CHT].mkv", torrent_hash=H2, size=700_000_000, probe=None)
    assert _rank_for_keep(loli) < _rank_for_keep(cht)               # 同画质比体积：繁中的大

    loli.ledger = _row(H1, LOLI_MIKAN)
    assert _rank_for_keep(loli) > _rank_for_keep(cht)               # 简体压过繁体


def test_end_to_end_a_pinned_lolihouse_release_seals_its_slot_when_unprobed(lib):
    """钉着 `ma:S01E08` 的 LoliHouse（抓取行记着番组页标题）、探测不可用：以前只能报 `seal_unknown`、这一集
    悬着；现在按番组页标题复核通过、封存这一集，另一份繁中的照常判输。"""
    s1 = lib.show("尼古喵喵").season(1)
    loli = s1.single(LOLI_FILE, size=560_000_000, tags="ma:S01E08", hash=H1)       # 不给 probe = 探测不可用
    cht = s1.single("[X] Yani Neko - 08 [1080p][CHT].mkv", size=700_000_000, hash=H2,
                    probe=video("h264", subs=["chi 繁體中文"]))
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H1, mikan_title=LOLI_MIKAN, season=1, episode=8, run_id="g0")

    c = lib.cycle(detectors=[DuplicateEpisodeDetector], dry_run=True)

    assert "seal_unknown" not in c.kinds()
    [t] = c.actions("trash")
    assert t.path == str(cht.path) and "集位已封存" in t.evidence["reason"]
    assert t.action.args["keep_path"] == str(loli.path)
