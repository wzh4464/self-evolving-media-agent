"""封存时也按这部番的 `require_any` 核对番组页标题（出处账本），不只在抓取那一刻。

sidecar 的 `require_any`（「这部番只保留某个版本」，生产上尼古喵喵）以前只在抓取挑候选时用
（`preferences.with_requirement`）。之后的一切都不再问它：
- 在 `require_any` 登记之前抓的、钉着 `ma:` 的 TV 版，复核（`meets_requirements`，只看中文字幕）照样通过
  → 封存这一集 → 后来 AutoBangumi 下的邪竜解放版反倒被当输家清进隔离区；
- 删除关口 I4 同样只认"钉着 + 复核通过"，拒绝删那份 TV 版——判重换了方向也删不掉，这一集每轮都卡着。

现在：钉着的文件，账本里它的番组页标题**没有**这部番要求的任何一个词 → 不封存（`seal_failed`，写明缺什么）；
判重排序先看满不满足 `require_any`（名字证据 = 番组页标题 + 显示名 + 文件名），再比画质；删除关口的 I4 用同一个
判据（钉着、复核通过、而且番组页标题满足 `require_any` 才算封存）。账本里没有这一行的，照旧（名字里常常没有版本词，
凭内部名判"不满足"会误伤）。
"""
from __future__ import annotations

from harness import video

from media_agent import ledger
from media_agent.kernel import Action, Finding
from media_agent.plugins.builtin import DuplicateEpisodeDetector

SHOW = "尼古喵喵"
TV_MIKAN = "[G] 尼古喵喵 / Yani Neko - 10 [TV版][1080p][简繁内封]"
XIE_MIKAN = "[H] 尼古喵喵 / Yani Neko - 10 [邪竜解放版][720p][简繁内封]"
H_TV, H_XIE = "7" * 40, "8" * 40
CHI_1080 = video("hevc", height=1080, subs=["chi 简体中文"])
CHI_720 = video("hevc", height=720, subs=["chi 简体中文"])


def _scene(lib, *, ledger_rows=True):
    sh = lib.show(SHOW)
    sh.sidecar(require_any=["邪竜解放版"])
    s1 = sh.season(1)
    tv = s1.single("[G] Yani Neko - 10 [1080p].mkv", size=900_000_000, tags="ma:S01E10", hash=H_TV,
                   probe=CHI_1080)
    xie = s1.single("[H] Yani Neko - 10 [720p].mkv", size=500_000_000, hash=H_XIE, probe=CHI_720)
    if ledger_rows:
        with ledger.Ledger.open(lib.cfg.state_dir) as led:
            led.record_grab(infohash=H_TV, mikan_title=TV_MIKAN, season=1, episode=10, run_id="g0")
            led.upsert_backfill(infohash=H_XIE, source=ledger.AUTOBANGUMI, mikan_title=XIE_MIKAN,
                                season=1, episode=10)
    return tv, xie


def test_a_pinned_release_without_the_required_version_is_not_sealed(lib):
    tv, xie = _scene(lib)

    c = lib.cycle(detectors=[DuplicateEpisodeDetector])

    [sf] = [f for f in c.findings if f.kind == "seal_failed"]
    assert sf.path == str(tv.path) and "邪竜解放版" in sf.summary
    [t] = c.applied("trash")                               # 删除关口放行：它不再算封存
    assert t["args"]["path"] == str(tv.path) and t["args"]["keep_path"] == str(xie.path)
    assert xie.path.exists() and not tv.path.exists()


def test_without_ledger_rows_the_pin_still_seals(lib):
    """对照：账本里没有它们——照旧，钉着的 TV 版封存这一集（内部名里没有版本词，不能凭它判"不满足"）。"""
    tv, xie = _scene(lib, ledger_rows=False)

    c = lib.cycle(detectors=[DuplicateEpisodeDetector], dry_run=True)

    assert "seal_failed" not in c.kinds()
    [t] = c.actions("trash")
    assert t.path == str(xie.path) and "集位已封存" in t.evidence["reason"]


def test_the_gate_agrees_and_still_guards_a_satisfying_seal(lib):
    """删除关口按同一个判据：手写的删除对准钉着、番组页标题满足 `require_any` 的那份——照旧 I4 拒绝。"""
    sh = lib.show(SHOW)
    sh.sidecar(require_any=["邪竜解放版"])
    s1 = sh.season(1)
    good = s1.single("[H] Yani Neko - 11 [720p].mkv", tags="ma:S01E11", hash=H_XIE, probe=CHI_720)
    s1.local("尼古喵喵 S01E11 [BD].mkv")
    with ledger.Ledger.open(lib.cfg.state_dir) as led:
        led.record_grab(infohash=H_XIE, mikan_title=XIE_MIKAN.replace("- 10", "- 11"), season=1,
                        episode=11, run_id="g0")
    f = Finding(rule="manual", kind="manual", severity="important", summary="手写删除", show=SHOW,
                path=str(good.path), torrent_hash=H_XIE,
                action=Action(op="trash", args={"path": str(good.path), "torrent_hash": H_XIE}))

    rep = lib.apply([f])

    [skip] = rep.skipped
    assert skip["reason"].startswith("删除关口：I4") and good.path.exists()
