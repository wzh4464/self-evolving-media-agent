"""订阅模式（`AB_MODE=subscription`）下本项目不再替 AutoBangumi 做事（`abmode`）：

- **抓取不打 `ab:` 标签**：`full` 下抓来的集在 AB 会算出同一个集号时带 `ab:<订阅 id>`，好让 AB 认领；订阅模式下 AB 不改名，
  标签只会让"AB 在订阅之外加了种子"的核对（`abmode.activity`）把本项目抓的认成 AB 的。
- **永远不叫 AB 刷新**：`refresh_all` / `refresh` 会让 AB 当场拉 RSS、下载——两个开关都关了照样下（ab 调研 §3.4：
  `/rss/refresh` 不看开关）。以前只有 `fix_title_aliases` / `repoint_rss` 调它；规则在订阅模式下已经停了（诊断不再提议），
  执行器这里再拦一道：从别处来的同名动作（手动 apply、演进规则、之前诊断的快照）也不改 AB 库、不停容器、不刷新。
"""
from __future__ import annotations

from harness import MikanItem, weekly

from media_agent.kernel import Action, Finding

TITLE = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"


def _grab_scene(lib):
    lib.configure(qbit_allow_empty=True)
    sh = lib.show("尼古喵喵")
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"尼古喵喵 S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(mikan_id="3500", bangumi_id=32, seasons={"1": {"have": list(range(1, 9))}})
    item = MikanItem(title=TITLE, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=["尼古喵喵"])
    return item


def test_full_mode_grab_still_tags_for_ab(lib):
    item = _grab_scene(lib)
    lib.cycle()
    assert {t.strip() for t in lib.qbit.torrent(item.infohash)["tags"].split(",")} == {"ma:S01E09", "ab:32"}


def test_subscription_mode_grab_does_not_tag_for_ab(lib):
    lib.configure(ab_mode="subscription")
    item = _grab_scene(lib)
    lib.cycle()
    assert lib.qbit.torrent(item.infohash)["tags"] == "ma:S01E09"


# ---------------------------------------------------------------- 不叫 AB 刷新
def _fix(lib) -> Finding:
    lib.bangumi(id=7, official_title="上伊那牡丹", title_raw="Sake o Tsugu",
                save_path=str(lib.media_root / "上伊那牡丹" / "Season 1"), group_name="ANi",
                rss_link="https://mikanani.me/RSS/Search?searchstr=Sake+o+Tsugu")
    return Finding(rule="title-match-broken", kind="subscription_broken", severity="important",
                   summary="订阅失效", show="上伊那牡丹",
                   action=Action(op="fix_title_aliases", args={"bangumi_id": 7, "aliases": ["Yoeru Sugata"]}))


def _repoint(lib) -> Finding:
    return Finding(rule="source-abandoned", kind="stale_rss_query", severity="important", summary="换链接",
                   show="上伊那牡丹", action=Action(op="repoint_rss", args={
                       "bangumi_id": 7, "rss_link": "https://mikanani.me/RSS/Bangumi?bangumiId=3500&subgroupid=583",
                       "aliases": ["Yoeru Sugata"]}))


def test_subscription_mode_never_writes_ab_or_asks_it_to_refresh(lib):
    lib.configure(ab_mode="subscription")
    fix = _fix(lib)
    before = lib.abdb.query("SELECT title_aliases, rss_link FROM bangumi WHERE id=7")

    rep = lib.apply([fix, _repoint(lib)])

    assert [r["op"] for r in rep.skipped] == ["fix_title_aliases", "repoint_rss"]
    assert all("订阅模式" in r["reason"] for r in rep.skipped)
    assert "refresh_all" not in lib.ab.calls and "refresh" not in lib.ab.calls
    assert not lib.docker_log.exists() or lib.docker_log.read_text() == ""        # 没停过容器
    assert lib.abdb.query("SELECT title_aliases, rss_link FROM bangumi WHERE id=7") == before


def test_a_whole_run_in_subscription_mode_never_asks_ab_to_refresh(lib):
    """整轮 `run`（迭代到不动点）：订阅失效的现场摆在那里，订阅模式下既不提议也不刷新。"""
    lib.configure(ab_mode="subscription")
    _fix(lib)
    lib.show("上伊那牡丹").season(1).single("[ANi] Sake o Tsugu - 04.mp4", name="[ANi] Sake o Tsugu - 04")

    loop = lib.loop()

    assert not loop.applied("fix_title_aliases") and not loop.applied("repoint_rss")
    assert "refresh_all" not in lib.ab.calls


def test_full_mode_still_fixes_and_refreshes(lib):
    rep = lib.apply([_fix(lib)])
    [rec] = rep.applied
    assert rec["refreshed"] is True and "refresh_all" in lib.ab.calls
