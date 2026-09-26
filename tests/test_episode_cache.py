"""TMDB 分集表按 (tmdb_id, 季) 缓存：在播的季 6 小时、播完的季 7 天，取不到的负缓存 6 小时。

runloop 调研（2026-09-26，生产 170 部番）：一次 `diagnose` 43.98 秒，其中 episode-available 31.9 秒——
grab.py 对每个 sidecar 季（129 个 sidecar、159 个季键）**不带缓存**地调 `season_episodes`，第二遍热跑照样 32 秒。
incomplete-season / source-abandoned 早就用着 `tmdbeps:` 缓存，抓取没用。一轮 `run` 要迭代到不动点（最多 3 次），
每次都这样就是每轮多 1–2 分钟、几百次 TMDB 请求。

还有两处不对：source-abandoned 取失败时把 `{"eps": []}` 当真数据写进缓存 6 小时——incomplete-season 与 sidecar-sync
随后读到"这一季没有集"；而取不到的季每次都重新问（TMDB 挂着时每个季键各等一次 20 秒超时）。
"""
from __future__ import annotations

import sqlite3

import httpx
import pytest

from harness import weekly
from media_agent import cache as cache_mod

SHOW = "尼古喵喵"
TID = 1234


def _calls(lib) -> list:
    return [c for c in lib.tmdb.calls if c[0] == "season_episodes"]


def _airing(lib, n_have: int = 9):
    """第 1 季周播 12 集、60 天前开播：第 9 集 4 天前播出，第 10 集还没播。本地已有 1..n_have。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, n_have + 1):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    sh.tmdb(TID, seasons={1: weekly(12, first_days_ago=60)})
    sh.sidecar(tmdb_id=TID, tmdb_title=SHOW, seasons={"1": {"have": list(range(1, n_have + 1))}})
    return sh


def _ended(lib):
    """第 1 季 12 集全在 200 天前开播：最后一集也播完 100 多天了。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 13):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    sh.tmdb(TID, seasons={1: weekly(12, first_days_ago=200)})
    sh.sidecar(tmdb_id=TID, tmdb_title=SHOW, seasons={"1": {"have": list(range(1, 13))}})
    return sh


def _age(lib, prefix: str, seconds: float) -> None:
    """把缓存里以 `prefix` 开头的 TMDB 条目往前挪 `seconds` 秒（模拟时间过去了）。"""
    conn = sqlite3.connect(str(lib.cfg.cache_db))
    conn.execute("UPDATE tmdb SET ts = ts - ? WHERE key LIKE ?", (seconds, prefix + "%"))
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ 命中缓存
def test_grab_reuses_the_episode_cache_so_a_second_diagnose_asks_tmdb_nothing(lib):
    _airing(lib)

    lib.diagnose()
    first = len(_calls(lib))
    lib.diagnose()

    assert first == 1                    # incomplete-season 取一次、缓存；抓取以前再不带缓存地问一次
    assert len(_calls(lib)) == first     # 第二遍（= run 的第二次迭代）一次都不问


def test_airing_season_is_refetched_after_six_hours(lib):
    _airing(lib)
    lib.diagnose()
    _age(lib, "tmdbeps:", 7 * 3600)

    lib.diagnose()

    assert len(_calls(lib)) == 2


def test_ended_season_is_kept_for_seven_days(lib):
    _ended(lib)
    lib.diagnose()

    _age(lib, "tmdbeps:", 2 * 86400)
    lib.diagnose()
    assert len(_calls(lib)) == 1         # 播完的季：两天前的分集表照用

    _age(lib, "tmdbeps:", 6 * 86400)     # 共 8 天
    lib.diagnose()
    assert len(_calls(lib)) == 2


def test_episodes_ttl_rule():
    today = __import__("datetime").date.today()
    aired = [{"episode_number": n, "air_date": d} for n, d in weekly(12, first_days_ago=200)]
    airing = [{"episode_number": n, "air_date": d} for n, d in weekly(12, first_days_ago=60)]
    undated = [*aired[:-1], {"episode_number": 12, "air_date": ""}]
    recent = [{"episode_number": n, "air_date": d} for n, d in weekly(12, first_days_ago=80)]

    assert cache_mod.episodes_ttl(aired, today) == cache_mod.EPISODES_ENDED_TTL
    assert cache_mod.episodes_ttl(airing, today) == cache_mod.EPISODES_TTL      # 还有没播的集
    assert cache_mod.episodes_ttl(undated, today) == cache_mod.EPISODES_TTL     # 没定档的集
    assert cache_mod.episodes_ttl([], today) == cache_mod.EPISODES_TTL          # 刚公布、还没有集
    # 最后一集 3 天前才播：也许只是 TMDB 还没把后面的集登上去，按在播算
    assert cache_mod.episodes_ttl(recent, today) == cache_mod.EPISODES_TTL


def test_sidecar_sync_reads_an_ended_season_with_the_ended_ttl(lib):
    """sidecar-sync 只读缓存（不自己问 TMDB）。它以前按 6 小时读：播完的季缓存 7 天，6 小时之后它就读不到、
    把 total / aired 从档案里拿掉——下一轮又加回来，每轮都写一次档案。"""
    sh = _ended(lib)
    lib.cycle()                                   # 写好档案：带 total / aired
    assert lib.sidecar(SHOW).seasons["1"].get("total") == 12

    _age(lib, "tmdbeps:", 2 * 86400)
    c = lib.cycle()

    assert not c.actions("write_sidecar")
    assert lib.sidecar(SHOW).seasons["1"].get("total") == 12
    assert sh.path.is_dir()


# ------------------------------------------------------------------ 取不到
def _failing(lib, exc):
    calls = []

    def season_episodes(tv_id, season):
        calls.append((tv_id, season))
        raise exc

    lib.tmdb.season_episodes = season_episodes
    return calls


@pytest.mark.allow("log_failure", match="集表读取失败")
def test_failure_is_negative_cached_for_six_hours(lib):
    _airing(lib)
    calls = _failing(lib, httpx.ReadTimeout("timed out"))

    lib.diagnose()
    n = len(calls)
    lib.diagnose()
    assert n == 1 and len(calls) == 1               # 6 小时内不再问；以前每个检测器、每一遍各问一次

    _age(lib, "tmdbepsfail:", 7 * 3600)
    lib.diagnose()
    assert len(calls) == 2


@pytest.mark.allow("log_failure", match="集表读取失败")
def test_failure_is_said_every_time_it_is_in_effect(lib):
    """负缓存不等于不说：还在生效的每一遍照样说这一季没评估（悄悄停摆必须被看见）。"""
    _airing(lib)
    _failing(lib, httpx.ReadTimeout("timed out"))
    lib.diagnose()
    lib.logs.clear()

    lib.diagnose()

    assert any("集表读取失败" in m and "incomplete-season" in m for m in lib.logs)


@pytest.mark.allow("log_failure", match="集表读取失败")
def test_an_outage_stops_asking_tmdb_for_the_rest_of_the_context(lib):
    """TMDB 连不上（不是"这一季不存在"）：同一个 Context（= 一轮 run 的全部迭代）里没缓存的季不再问——
    以前每个季键各等一次超时（生产 159 个季键 × 20 秒）。"""
    lib.configure(qbit_allow_empty=True)
    for i, name in enumerate(("甲番", "乙番", "丙番")):
        sh = lib.show(name)
        sh.season(1).local(f"{name} S01E01.mkv")
        sh.tmdb(TID + i, seasons={1: weekly(12, first_days_ago=60)})
        sh.sidecar(tmdb_id=TID + i, tmdb_title=name, seasons={"1": {"have": [1]}})
    calls = _failing(lib, httpx.ConnectTimeout("connect timed out"))

    ctx = lib.context()
    lib.diagnose(ctx=ctx)

    assert len(calls) == 1


def test_a_missing_season_does_not_stop_the_others(lib):
    """TMDB 回 404（这一季在 TMDB 上不存在）是这一个季键的答案，不是 TMDB 挂了：别的季照问。"""
    lib.configure(qbit_allow_empty=True)
    sh = lib.show(SHOW)
    sh.season(1).local(f"{SHOW} S01E01.mkv")
    sh.season(2).local(f"{SHOW} S02E01.mkv")
    sh.tmdb(TID, seasons={2: weekly(12, first_days_ago=60)})     # 没有第 1 季
    sh.sidecar(tmdb_id=TID, tmdb_title=SHOW, seasons={"1": {"have": [1]}, "2": {"have": [1]}})
    lib.tripwire.allow("log_failure", match="第 1 季集表读取失败")

    lib.diagnose()

    asked = {c[2] for c in _calls(lib)}
    assert asked == {1, 2}


@pytest.mark.allow("log_failure", match="集表读取失败")
def test_source_abandoned_failure_no_longer_poisons_the_cache_with_an_empty_list(lib):
    """source-abandoned（订阅检测器里排在 incomplete-season 前面）取失败时照旧按"没有已播的集"往下走，但以前把
    `{"eps": []}` 当真数据写进缓存：之后 6 小时 incomplete-season / sidecar-sync 读到的都是"这一季一集都没有"。"""
    sh = _airing(lib)
    rss = "https://mikanani.me/RSS/Bangumi?bangumiId=3500&subgroupid=583"
    sh.bangumi(7, title_raw="Yani Neko", rss_link=rss, group_name="LoliHouse")
    lib.web.rss(rss, [f"[LoliHouse] Yani Neko - {n:02d} [WebRip 1080p]" for n in range(1, 10)])
    calls = _failing(lib, httpx.ReadTimeout("timed out"))

    lib.diagnose()

    assert calls
    c = cache_mod.Cache(lib.cfg.cache_db)
    assert c.get_tmdb(f"tmdbeps:{TID}:1", ttl=10**9) is None     # 失败不当成"没有集"缓存
    assert c.get_episodes(TID, 1) is None


# ------------------------------------------------------------------ 报错文本里不带 api_key
def _http_error(status: int, key: str = "SECRETKEY123") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", f"https://api.themoviedb.org/3/tv/999?api_key={key}&language=zh-CN")
    try:
        httpx.Response(status, request=req).raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("raise_for_status 没抛")


def test_brief_keeps_the_status_and_drops_the_url():
    """httpx 的 HTTPStatusError 文本带着整个请求 URL（`TMDBClient._get` 把 `api_key` 放在查询参数里）：
    `_brief` 只留状态码。别的异常文本里碰巧有 `api_key=` 的，也遮掉。"""
    e = _http_error(503)
    assert "SECRETKEY123" in str(e)                               # 前提：原文确实带着
    assert cache_mod._brief(e) == "HTTPStatusError: HTTP 503"
    odd = RuntimeError("GET https://api.themoviedb.org/3/search/tv?api_key=SECRETKEY123&query=x failed")
    assert "SECRETKEY123" not in cache_mod._brief(odd) and "RuntimeError" in cache_mod._brief(odd)
    assert cache_mod._brief(httpx.ConnectTimeout("connect timed out")) == "ConnectTimeout: connect timed out"


def test_rate_limiting_counts_as_an_outage_but_a_missing_season_does_not():
    """429（限流）与 5xx、超时一样是"TMDB 此刻不行"——同一轮里别的季也别再问；404 只是这一季的答案
    （2026-09-27 审查：把 429 当成这一季答案的变异存活）。"""
    assert cache_mod._outage(_http_error(429))
    assert cache_mod._outage(_http_error(503))
    assert cache_mod._outage(httpx.ConnectTimeout("connect timed out"))
    assert not cache_mod._outage(_http_error(404))
