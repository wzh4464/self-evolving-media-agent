"""判重 / 改名的集位先问出处账本，再看文件名（`builtin._resolve` 与 `builtin.ledger_view`）。

**2026-08-31 Re:Zero 现场**：TMDB 把四季压平成一个 Season 1，库里也只有 `Season 1`。AutoBangumi 把
`[Fyy Raws] … 3rd Season - 08` 改名成 `Re:从零开始的异世界生活 S01E08.mkv`（半角冒号），与 2016 年真正的第 8 集
`Re：从零开始的异世界生活 S01E08.mkv`（全角冒号、1.31 GB）撞进同一个集位；分类交接之后判重按名字取舍，把原片清进了
隔离区。第 2 阶段之后隔离可回退、purge 不会硬删，但隔离本身照样发生（`test_quarantine_disposal.py` 的
`test_rezero_end_to_end_the_original_survives_the_retention` 仍然演示着它）。

账本记着这个种子在 AB 库里登记的番组页标题：发布方明写"第三季"。规则：
- 有种子、没钉子的文件，账本（番组页标题）按此刻的 `season_offsets` 算出的集位与文件名不同 → **按账本**
  （配了 `{"3": 50}`：它是 S01E58，改名提议改回 S01E58，不进 S01E08 的桶）；
- 账本说"发布方声明的是别的季、换算不了"：文件名说的集位不可信——它**不参与判重**（既不当保留方也不当输家），
  报 `season_numbering_conflict` 让人登记换算关系。只在它与别的文件撞进同一个集位时才报：
  单独一个的（生产上正相反的你与我 12 个 `第二季 - 13…23` 已按连续编号改好名）照旧按文件名认，不制造噪音；
- 多文件种子（合集、合并发布）的番组页标题说不了单个文件是哪一集：账本不参与；撤销了的行不参与。
"""
from __future__ import annotations

import argparse

import pytest
from harness import video

from media_agent import cli, history, ledger
from media_agent import ledger_backfill as lb
from media_agent.plugins.builtin import (DuplicateEpisodeDetector, UnrenamedDetector, _resolve,
                                         ledger_view)

REZERO = "Re：从零开始的异世界生活"
AB_NAME = "Re:从零开始的异世界生活 S01E08.mkv"          # AB 按错的口径改出来的名字（半角冒号）
FYY_MIKAN = ("[Fyy Raws] Re:从零开始的异世界生活 第三季 / Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season"
             " - 08 [1080p][AVC AAC]")
FYY_FILE = "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p][AVC AAC].mp4"
H_AB = "c" * 40
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")


def _url(h: str) -> str:
    return f"https://mikanani.me/Download/20260831/{h}.torrent"


def _rezero(lib, *, offsets=None, name=FYY_FILE):
    """2016 年的原片（纯本地、1.41 GB、零字幕轨）+ AB 下的第三季第 8 集（已交接到剧名分类，已被 AB 改成 S01E08）。"""
    sh = lib.show(REZERO)
    if offsets is not None:
        sh.sidecar(season_offsets=offsets)
    s1 = sh.season(1)
    real = s1.local(f"{REZERO} S01E08.mkv", size=1_410_655_350, probe=RAW)
    sh.bangumi(9, title_raw="Re Zero kara Hajimeru Isekai Seikatsu 3rd Season", season=1)
    lib.ab_rows("torrent", [{"bangumi_id": 9, "name": FYY_MIKAN, "url": _url(H_AB), "downloaded": 1}])
    ab = s1.single(AB_NAME, size=487_000_000, name=name, hash=H_AB, tags="ab:9", probe=CHI)
    return real, ab


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False,
                accept_torrent_count=False, run=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ------------------------------------------------------------------ 2026-08-31 端到端
def test_rezero_2026_08_31_the_original_is_not_quarantined(offline_cli, capsys):
    """整条 `run`：开头的补录从 AB 库认出这个种子是「第三季 - 08」，判重不再拿它和 2016 年的第 8 集比。"""
    lib = offline_cli
    real, ab = _rezero(lib)
    ident = lib.ident(real)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    assert real.exists() and lib.ident(real) == ident             # 原片还在原地
    assert not any(lib.ident(p) == ident for p in lib.trash_files())
    assert not [r for r in lib.audit() if r["op"] == "trash"]
    [snap] = history.load_snapshots(lib.cfg.state_dir)
    conflict = [r for r in snap.findings if r["kind"] == "season_numbering_conflict"]
    assert [r["path"] for r in conflict] == [str(ab.path)]
    assert conflict[0]["subject"] == "S01E08" and "第 3 季" in conflict[0]["summary"]


def test_rezero_with_offsets_the_file_is_the_58th_episode(lib):
    """生产上 sidecar 配着 `{"3": 50}`：账本按它算出 S01E58——改名提议改回 S01E58，S01E08 的桶里只有原片。"""
    real, ab = _rezero(lib, offsets={"3": 50})
    lb.backfill(lib.context())

    c = lib.cycle(detectors=[UnrenamedDetector, DuplicateEpisodeDetector], dry_run=True)

    assert not c.actions("trash")
    [ren] = c.actions("rename")
    assert ren.path == str(ab.path)
    assert ren.action.args["new_name"] == f"{REZERO} S01E58.mkv"
    assert "season_numbering_conflict" not in c.kinds()


def test_without_the_ledger_the_old_behaviour_stands(lib):
    """对照：账本里没有它（没补录）——按文件名，今天的行为不变（原片被当输家隔离，purge 那一侧兜底）。"""
    real, ab = _rezero(lib)

    c = lib.cycle(detectors=[DuplicateEpisodeDetector], dry_run=True)

    [t] = c.actions("trash")
    assert t.path == str(real)


# ------------------------------------------------------------------ 单元：ledger_view / _resolve
def _file(lib, state, path):
    for s in state.shows:
        for f in s.files:
            if f.path == path:
                return s, f
    raise AssertionError(f"扫描里没有 {path}")


def test_a_lone_conflicting_file_keeps_its_name_slot_and_is_not_reported(lib):
    """正相反的你与我：ANi 的 `第二季 - 15` 已按连续编号改成 S01E15，sidecar 没配换算。它一个人占着 S01E15，
    照旧按文件名认——不报冲突、不改名（生产上 12 个这样的文件）。"""
    show = "正相反的你与我"
    sh = lib.show(show)
    sh.bangumi(6, title_raw="Seihantai na Kimi to Boku", season=1)
    mikan = "[ANi] Seihantai na Kimi to Boku S02 /  相反的你和我 第二季 - 15 [1080P][Baha][WEB-DL][AAC AVC][CHT]"
    lib.ab_rows("torrent", [{"bangumi_id": 6, "name": mikan, "url": _url(H_AB)}])
    t = sh.season(1).single(f"{show} S01E15.mp4", name="[ANi] 相反的你和我 第二季 - 15 [1080P].mp4",
                            hash=H_AB, tags="ab:6")
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert f.ledger is not None and ledger_view(f, s) == ("conflict", None)
    assert _resolve(f, s) == (1, 15)
    fs = lib.diagnose(state, detectors=[UnrenamedDetector, DuplicateEpisodeDetector])
    assert fs == []


def test_a_grabbed_row_is_the_grabbers_slot(lib):
    """抓取行的集位是抓取器定的（与钉子同源）：钉子丢了（人改过标签），声明了季号的发布照样按它认。"""
    s1 = lib.show(REZERO).season(1)
    t = s1.single(AB_NAME, size=487_000_000, name=FYY_FILE, hash=H_AB)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_AB, mikan_title=FYY_MIKAN, season=1, episode=58, run_id="g1")

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("slot", (1, 58)) and _resolve(f, s) == (1, 58)


@pytest.mark.parametrize("case", ["retracted", "multi-video", "no-declared-season"])
def test_the_ledger_stays_out_when_it_cannot_speak_for_the_file(lib, case):
    s1 = lib.show(REZERO).season(1)
    title = FYY_MIKAN if case != "no-declared-season" else "[G] Re Zero - 58 [1080p]"
    if case == "multi-video":
        t = s1.torrent({AB_NAME: 487_000_000, "Re:从零开始的异世界生活 S01E09.mkv": 487_000_000},
                       name="[G] Re Zero 08-09", layout="nosub", hash=H_AB)
        path = t.paths[0]
    else:
        t = s1.single(AB_NAME, size=487_000_000, name=FYY_FILE, hash=H_AB)
        path = t.path
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.upsert_backfill(infohash=H_AB, source=ledger.AUTOBANGUMI, mikan_title=title,
                            show_dir=str(lib.path(REZERO)))
        if case == "retracted":
            led.retract(H_AB, run_id="rb", why="测试")

    state = lib.scan()
    s, f = _file(lib, state, path)
    assert ledger_view(f, s) == ("", None)
    assert _resolve(f, s) == (1, 8)                               # 按文件名，与没有账本时一样


def test_a_broken_ledger_falls_back_and_says_so(lib):
    real, ab = _rezero(lib)
    ledger.path_of(lib.cfg.state_dir).write_bytes(b"junk" * 100)

    state = lib.scan()

    assert state.ledger_problem and ledger.LEDGER_NAME in state.ledger_problem
    assert all(f.ledger is None for s in state.shows for f in s.files)


# ------------------------------------------------------------------ 账本只在文件所在的库内季里换算（2026-09-27 审查）
GIRLS = "超超超超超喜欢你的100个女朋友"
NIX_GIRLS = (f"[Nix-Raws] {GIRLS} 第三季 / Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no S01E25 "
             f"[CR WEB-DL 1080p AVC AAC][MKV]")


def test_100_girlfriends_s3_keeps_its_ab_numbering(lib):
    """生产快照：AB 订阅 37 是第三季、`episode_offset -24`，把 `… S01E25` 改名成 `Season 3/… S03E01.mkv`。番组页标题
    `第三季 / … S01E25` 里发布方同时写了第 3 季与第 1 季（TMDB 的连续编号）。以前账本按标题里的 `S01` 定季、声明的季取
    最小的 1，算出 (1, 25)、压过文件名——10 个名字正确的文件被提议改成 `S01E25`…`S01E36`，sidecar 的 S3 进度被清掉。
    声明了不止一个季号的标题说不清是哪一季的编号：账本不说话，按文件名（与 AB 一致）。"""
    sh = lib.show(GIRLS)
    sh.bangumi(37, title_raw="Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo", season=3,
               episode_offset=-24)
    lib.ab_rows("torrent", [{"bangumi_id": 37, "name": NIX_GIRLS, "url": _url(H_AB)}])
    t = sh.season(3).single(f"{GIRLS} S03E01.mkv", hash=H_AB, tags="ab:37",
                            name="[Nix-Raws] Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo S01E25 "
                                 "[CR WEB-DL 1080p AVC AAC].mkv")
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("", None) and _resolve(f, s) == (3, 1)
    assert lib.diagnose(state, detectors=[UnrenamedDetector]) == []
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        row = led.get(H_AB)
    assert row.slot is None and row.declared_season is None    # 补录也不替它定一个集位


def test_a_flattened_show_maps_the_release_into_the_files_own_season_like_grab_does(lib):
    """Re:Zero（TMDB 压平成一季，sidecar `{2: 25, 3: 50, 4: 66}`）：`第四季 / … S04E15` 在 Season 1 是第 81 集——
    抓取（`grab._slot_in_season`）一直这么算；账本以前让标题里的 `S04` 定季，算出 (4, 15)。"""
    from media_agent.plugins.grab import _slot_in_season

    title = "[Nix-Raws] Re:从零开始的异世界生活 第四季 / Re:Zero kara Hajimeru Isekai Seikatsu S04E15 [CR WEB-DL 1080p]"
    offsets = {"2": 25, "3": 50, "4": 66}
    sh = lib.show(REZERO)
    sh.sidecar(season_offsets=offsets)
    sh.bangumi(9, title_raw="Re Zero kara Hajimeru Isekai Seikatsu", season=1)
    lib.ab_rows("torrent", [{"bangumi_id": 9, "name": title, "url": _url(H_AB)}])
    t = sh.season(1).single("Re:从零开始的异世界生活 S01E15.mkv", hash=H_AB, tags="ab:9",
                            name="[Nix-Raws] Re Zero kara Hajimeru Isekai Seikatsu S04E15 [CR WEB-DL 1080p].mkv")
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("slot", (1, 81))
    assert _slot_in_season(title, 15, 1, {int(k): v for k, v in offsets.items()}) == (81, "")


def test_a_special_in_season_0_gets_no_main_season_offset(lib):
    """`第三季 OVA - 01` 放在 Season 0 是特典第 1 集：季号偏移只换算进正片季（抓取一直如此）。以前账本把第 3 季的
    偏移 24 加上去、改名提议 S00E25（药屋少女、Re:Zero 生产上都有 Season 0 + season_offsets）。"""
    sh = lib.show("辉夜大小姐想让我告白")
    sh.sidecar(season_offsets={"3": 24})
    sh.bangumi(50, title_raw="Kaguya-sama", season=0)
    lib.ab_rows("torrent", [{"bangumi_id": 50, "name": "[G] 辉夜大小姐想让我告白 第三季 OVA - 01 [1080p]",
                             "url": _url(H_AB)}])
    t = sh.season(0).single("辉夜大小姐想让我告白 S00E01.mkv", name="[G] Kaguya-sama OVA - 01 [1080p].mkv",
                            hash=H_AB, tags="ab:50")
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("slot", (0, 1))
    assert lib.diagnose(state, detectors=[UnrenamedDetector]) == []


def test_a_main_episode_sitting_in_season_0_is_a_conflict(lib):
    """LAT-03 的形态：第三季的正片（没有 OVA / 特别篇字样）躺在 Season 0——账本说它的名字不可信（与抓取同一个判据）。"""
    sh = lib.show("辉夜大小姐想让我告白")
    sh.sidecar(season_offsets={"3": 24})
    title = "[LoliHouse] 辉夜大小姐想让我告白 第三季 / Kaguya-sama wa Kokurasetai S3 - 03 [WebRip 1080p]"
    sh.bangumi(50, title_raw="Kaguya-sama", season=0)
    lib.ab_rows("torrent", [{"bangumi_id": 50, "name": title, "url": _url(H_AB)}])
    t = sh.season(0).single("辉夜大小姐想让我告白 S00E03.mkv", name="[LoliHouse] Kaguya-sama S3 - 03.mkv",
                            hash=H_AB, tags="ab:50")
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("conflict", None)


def test_ab_episode_offset_only_applies_to_the_subscriptions_own_season(lib):
    """AB 订阅 37 是第三季（-24）；Season 1 里的 `第一季 - 05` 不是它下的，不减 24（以前减成负数、判成 conflict）。"""
    sh = lib.show(GIRLS)
    sh.bangumi(37, title_raw="Kimi no Koto ga", season=3, episode_offset=-24)
    lib.ab_rows("torrent", [{"bangumi_id": 37, "name": f"[G] {GIRLS} 第一季 - 05 [1080p]", "url": _url(H_AB)}])
    t = sh.season(1).single(f"{GIRLS} S01E05.mkv", name="[G] Kimi no Koto ga - 05 [1080p].mkv", hash=H_AB)
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, t.path)
    assert ledger_view(f, s) == ("slot", (1, 5))


def test_a_pinned_seal_takes_part_in_its_own_episodes_dedupe(lib):
    """2026-08-31 之前的抓取：钉着 `ma:S01E58`、AB 也登记过（补录成 AB 行、番组页标题「第三季 - 08」、没有偏移）、被 AB
    改名成 `S01E08`。钉子是定论：它在 S01E58 的桶里，不能因为账本"换算不了第 3 季"就被剔出去、报一条说它名字不可信的
    `season_numbering_conflict`——那样这一集的判重永远做不成（2026-09-27 审查）。"""
    sh = lib.show(REZERO)
    s1 = sh.season(1)
    sh.bangumi(9, title_raw="Re Zero kara Hajimeru Isekai Seikatsu", season=1)
    lib.ab_rows("torrent", [{"bangumi_id": 9, "name": FYY_MIKAN, "url": _url(H_AB), "downloaded": 1}])
    fyy = s1.single(AB_NAME, size=487_000_000, name=FYY_FILE, hash=H_AB, tags="ab:9, ma:S01E58", probe=CHI)
    other = s1.single(f"{REZERO} S01E58.mkv", size=400_000_000, tags="ab:9",
                      name="[LoliHouse] Re Zero - 58 [WebRip 1080p HEVC-10bit AAC].mkv",
                      probe=video("hevc", subs=["chi 简体中文"]))
    lb.backfill(lib.context())

    state = lib.scan()
    s, f = _file(lib, state, fyy.path)
    assert ledger_view(f, s) == ("slot", (1, 58))
    c = lib.cycle(detectors=[DuplicateEpisodeDetector], dry_run=True)
    assert "season_numbering_conflict" not in c.kinds()
    assert [t.path for t in c.actions("trash")] == [str(other.path)]     # 钉着的封存这一集，另一份判输
