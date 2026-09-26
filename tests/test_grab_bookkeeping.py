"""回归测试：抓取的记账必须写成，且不被同轮的 sidecar 写回覆盖。

两个都是 2026-09-26 从审计日志里查出来的真事，共同点是**下载本身没坏，
坏的是记账**——所以从外面看一切正常，只有翻日志才发现。

一、`_op_grab_episode` 最后一行 `resp.status_code` 引用了重构后已不存在的
    变量（`a95cdfc` 把手写 HTTP 换成 `qbit.add_torrent()`，返回 bool）。
    种子加进去了、名也改了、sidecar 也写了，最后写审计时抛 NameError，
    被执行器兜住记成 `failed`。后果不是丢下载，是**丢记账**：
    9-16 到 9-26 共 12 次抓取全部记成失败，一条 `applied` 都没有，
    于是这些抓取**没有 undo 记录、回退不了**。

二、`_op_write_sidecar` 是整份覆盖，payload 却是**诊断阶段**算的快照；
    抓取（op 0）排在它（op 10）之前已经往 sidecar 写了新集，于是被盖掉。
    实测「躲在超市后门抽烟的两人」S01E12 文件已落盘、`have` 仍停在 11，
    同一集被连抓两三次（靠 qBittorrent 的 infohash 去重才没真重下）。

    修法是只并回**本轮自己抓的**那几集。不能无脑并磁盘旧值：sidecar-sync
    的职责之一正是把已删掉的集从 `have` 里摘掉，求并集会让它再也摘不掉。

第一条原先是**切 actions.py 源码文本**来检查的（在 `def _op_grab_episode`
与 `def _infohash_v1` 之间找 `resp.`）——文件一拆分这个测试就会以 ValueError
崩掉而不是干净地失败。现在改成真的跑一次抓取：FakeWeb 提供 .torrent，
FakeQbit 接住加种，断言审计里是 `applied` 且带 `ungrab_episode` 逆操作。
任何让抓取以异常结束的回归（NameError 也好、别的也好）都会变成 `unknown`
（加种之后抛的，2026-09-26 第 3 阶段起）或 `failed`（加种之前抛的），被 tripwire 直接判红。

跑法：uv run pytest tests/test_grab_bookkeeping.py
"""
import pytest

from media_agent.kernel import Action, Finding

SHOW = "躲在超市后门抽烟的两人"
TITLE_12 = ("[LoliHouse] 躲在超市后门抽烟的两人 / Super no Ura de Yani Suu Futari - 12 "
            "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")


def grab_finding(show_dir, url, *, ep=12, title=TITLE_12, bangumi_id=None) -> Finding:
    return Finding(rule="episode-available", kind="episode_grabbable", severity="important",
                   summary=f"S01E{ep:02d} 可抓取", show=show_dir.name,
                   action=Action(op="grab_episode", args={
                       "url": url, "title": title, "show_dir": str(show_dir),
                       "season": 1, "episode": ep, "bangumi_id": bangumi_id,
                       "category": show_dir.name, "official_title": show_dir.name}))


def sidecar_finding(show_dir, payload: dict) -> Finding:
    return Finding(rule="sidecar-sync", kind="sidecar_stale", severity="minor",
                   summary="采集档案需更新", show=show_dir.name,
                   action=Action(op="write_sidecar",
                                 args={"show_dir": str(show_dir), "payload": payload}))


@pytest.fixture
def show(lib):
    sh = lib.show(SHOW)
    sh.season(1)
    return sh


def have_of(lib) -> list:
    return (lib.sidecar(SHOW).seasons.get("1") or {}).get("have") or []


# ---- 一：抓取必须记成 applied，带逆操作 ----
def test_grab_is_audited_as_applied_with_undo(lib, show):
    url, h = lib.web.torrent(TITLE_12)

    report = lib.apply([grab_finding(show.path, url)], run_id="g1")

    assert report.failed == []                          # 抓取以异常结束（如 NameError）会落在这里
    [rec] = [r for r in lib.audit("g1") if r["op"] == "grab_episode"]
    assert rec["status"] == "applied"                   # 抓取成功会写 applied 审计（才有 undo 可回退）
    assert rec["undo"] == {"op": "ungrab_episode", "show_dir": str(show.path),
                           "season": 1, "episode": 12, "title": TITLE_12,
                           "infohash": h}                    # 回退时把出处账本那一行标成撤销
    assert rec["already_present"] is False
    assert lib.qbit.has(h)
    assert have_of(lib) == [12]


def test_grab_of_already_present_torrent_still_records_have(lib, show):
    """qBittorrent 对已存在的 infohash 返回 409——那不是失败，是"已经有了"，
    同样要记进 have（Re:Zero 上曾把 409 误判成失败）。"""
    url, h = lib.web.torrent(TITLE_12)
    blob = lib.web.urlopen(url).read()
    lib.qbit.add_torrent(blob, save_path=str(show.path / "Season 1"), category=SHOW)

    report = lib.apply([grab_finding(show.path, url)])

    [rec] = report.applied
    assert rec["already_present"] is True
    assert have_of(lib) == [12]


def test_grab_rollback_only_forgets_have(lib, show):
    """ungrab 的语义是"当作没抓过、下轮可重抓"，**不动种子和文件**。"""
    url, h = lib.web.torrent(TITLE_12)
    lib.apply([grab_finding(show.path, url)], run_id="g1")

    res = lib.rollback("g1")

    assert (res["reverted"], res["failed"]) == (1, 0)
    assert have_of(lib) == []
    assert lib.qbit.has(h)


# ---- 二：同一批次里的 write_sidecar 不得盖掉本轮抓的集 ----
def test_grabbed_episode_survives_stale_sidecar_snapshot(lib, show):
    show.sidecar(seasons={"1": {"have": [10, 11]}})
    url, _ = lib.web.torrent(TITLE_12)
    # sidecar-sync 的 payload 是诊断阶段算的，那时 E12 还没抓 —— have 到 11。
    stale = {"canonical_title": SHOW, "seasons": {"1": {"have": [10, 11]}}}

    report = lib.apply([sidecar_finding(show.path, stale), grab_finding(show.path, url)])

    assert [r["op"] for r in report.applied] == ["grab_episode", "write_sidecar"]
    have = have_of(lib)
    assert 12 in have, f"本轮抓的集不被旧快照盖掉：have={have}"
    assert {10, 11} <= set(have), f"快照里本来就有的集保留：have={have}"


def test_removed_episode_is_still_dropped_even_in_a_grabbing_batch(lib, show):
    """反向守卫：**没被本轮抓过**的集，该由 sidecar-sync 摘掉就得摘掉，
    否则删掉一集之后 `have` 永远挂着它，抓取器再也不会补回来。"""
    show.sidecar(seasons={"1": {"have": [10, 11, 99]}})
    url, _ = lib.web.torrent(TITLE_12)
    stale = {"canonical_title": SHOW, "seasons": {"1": {"have": [10, 11]}}}

    lib.apply([sidecar_finding(show.path, stale), grab_finding(show.path, url)])

    assert have_of(lib) == [10, 11, 12]                 # 已经不存在的集仍会被摘掉（不是无脑并集）


def test_removed_episode_is_dropped_without_any_grab(lib, show):
    show.sidecar(seasons={"1": {"have": [10, 11, 99]}})
    lib.apply([sidecar_finding(show.path, {"canonical_title": SHOW,
                                           "seasons": {"1": {"have": [10, 11]}}})])
    assert 99 not in have_of(lib)


def test_aliases_recorded_at_grab_time_are_kept(lib, show):
    """别名只增不减：抓取时记下的发布名不能被快照冲掉。"""
    show.sidecar(aliases=["Super no Ura de Yani Suu Futari"])
    lib.apply([sidecar_finding(show.path, {"canonical_title": SHOW, "aliases": []})])
    assert "Super no Ura de Yani Suu Futari" in lib.sidecar(SHOW).aliases
