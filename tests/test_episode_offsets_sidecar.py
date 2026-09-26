"""sidecar 的 `episode_offsets`：按**库内季**换算发布名里的原始集号（critic N13 的前一半）。

AutoBangumi 订阅 id 37（《超超超超超喜欢你的100个女朋友》第三季）带着 `episode_offset -24`：这一季的发布按
连续集号编，`- 25` 是 S03E01。这个换算以前只活在 AB 库的订阅行上（`show.bangumi`）：

- AB 一退役（订阅行删掉、`deleted=1`），判重、改名、`have` 就都按 25 认，没有任何信号；
- 以前整部番一个值：Season 1 里的 `- 05` 也减 24、认不出（`_slot_from` / `have_episodes` 都这样），
  只有出处账本（`_ab_offset_for`）知道它只对订阅的那一季。

现在一处口径（`builtin.episode_offset_for`）：sidecar 里登记了这一季就按它（含 0：人说这一季不换算），
没登记时退回 AB 订阅行——只对 AB 下载落进的那一季（`Season <season + season_offset>`）。
"""
from __future__ import annotations

from media_agent import ledger
from media_agent.kernel import have_episodes
from media_agent.plugins.builtin import UnrenamedDetector, _resolve, episode_offset_for

TITLE = "超超超超超喜欢你的100个女朋友"
RAW_25 = "[Nekomoe kissaten] Hyakkano - 25 [1080p][JPSC].mkv"
RAW_05 = "[Nekomoe kissaten] Hyakkano - 05 [1080p][JPSC].mkv"


def _files(lib):
    state = lib.scan(resolve_tmdb=False)
    [show] = state.shows
    return show, {f.filename: f for f in show.files}


def test_the_sidecar_offset_alone_converts_the_raw_number(lib):
    """AB 那一行已经没了：sidecar 里的换算照样生效——判重的集位、`have`、改名目标一致。"""
    sh = lib.show(TITLE)
    sh.sidecar(episode_offsets={"3": -24})
    t = sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert episode_offset_for(show, 3) == -24 and episode_offset_for(show, 1) == 0
    assert _resolve(files[RAW_25], show) == (3, 1)
    assert have_episodes(show) == {3: {1}}
    [f] = lib.diagnose(detectors=[UnrenamedDetector])
    assert f.action.args["new_name"] == f"{TITLE} S03E01.mkv"
    assert f.torrent_hash == t.hash


def test_a_sidecar_entry_wins_over_the_autobangumi_row(lib):
    """人写的 `{"3": 0}` = 这一季不换算，盖过 AB 订阅行上的 -24（登记了就算数，0 也算）。"""
    sh = lib.show(TITLE)
    sh.sidecar(episode_offsets={"3": 0})
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert episode_offset_for(show, 3) == 0
    assert _resolve(files[RAW_25], show) == (3, 25)


def test_the_autobangumi_offset_only_applies_to_its_own_season(lib):
    """以前 `_slot_from` 与 `have_episodes` 对整部番减 24：Season 1 的 `- 05` 换算成 -19、认不出。"""
    sh = lib.show(TITLE)
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24)
    sh.season(1).single(RAW_05)
    sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert _resolve(files[RAW_05], show) == (1, 5)
    assert _resolve(files[RAW_25], show) == (3, 1)
    assert have_episodes(show) == {1: {5}, 3: {1}}


def test_the_autobangumi_season_offset_moves_the_offset_to_the_library_season(lib):
    """AB 的 `season_offset`：下载落进 `Season <season + season_offset>`（AB `_gen_save_path`），
    偏移跟着的是那个库内季，不是订阅行上的 `season`。"""
    sh = lib.show(TITLE)
    lib.bangumi(id=37, official_title=TITLE, title_raw="Hyakkano", season=2, season_offset=1,
                episode_offset=-24, save_path=str(sh.path / "Season 3"))
    sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert episode_offset_for(show, 3) == -24 and episode_offset_for(show, 2) == 0
    assert _resolve(files[RAW_25], show) == (3, 1)


def test_the_sidecar_keeps_the_offset_after_the_ab_row_is_disabled(lib):
    """AB 里停用订阅（`deleted=1`）之后 media-agent 读不到那一行（`AutoBangumiDB.bangumi` 只读 `deleted=0`）。"""
    sh = lib.show(TITLE)
    sh.sidecar(episode_offsets={"3": -24})
    sh.bangumi(37, title_raw="Hyakkano", season=3, episode_offset=-24, deleted=True)
    sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert show.bangumi is None
    assert _resolve(files[RAW_25], show) == (3, 1)


def test_the_ledger_reads_the_release_title_with_the_same_offset(lib):
    """出处账本从番组页标题重算集位（`ledger_view`）用同一个口径：AB 下的、已改成 `S03E01` 的文件，番组页标题
    `第三季 / … - 25` 按 sidecar 的 -24 还是第 1 集——以前只认 AB 行，AB 行没了就算成 (3, 25)、要改名成 S03E25。"""
    sh = lib.show(TITLE)
    sh.sidecar(episode_offsets={"3": -24})
    h = "d" * 40
    mikan = "[Nekomoe kissaten] 超超超超超喜欢你的100个女朋友 第三季 / Hyakkano S3 - 25 [1080p][JPSC]"
    sh.season(3).single(f"{TITLE} S03E01.mkv", name=RAW_25, hash=h)
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.upsert_backfill(infohash=h, source=ledger.AUTOBANGUMI, mikan_title=mikan)

    show, files = _files(lib)

    assert _resolve(files[f"{TITLE} S03E01.mkv"], show) == (3, 1)
    assert not lib.diagnose(detectors=[UnrenamedDetector])


def test_bad_sidecar_values_are_ignored_not_fatal(lib):
    """人手写错（不是数、非数字季号）不让判重崩：那一项按没写认。写成字符串的数照样认。"""
    sh = lib.show(TITLE)
    sh.sidecar(episode_offsets={"3": "minus 24", "x": -1, "1": "-2", "2": True})
    sh.season(3).single(RAW_25)

    show, files = _files(lib)

    assert episode_offset_for(show, 3) == 0
    assert episode_offset_for(show, 1) == -2
    assert episode_offset_for(show, 2) == 0
    assert _resolve(files[RAW_25], show) == (3, 25)
