"""磁盘缓存：TMDB 结果、内容哈希、LLM 判断。避免每轮重复付费/重复 IO。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tmdb (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS hash (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, digest TEXT);
CREATE TABLE IF NOT EXISTS llm (key TEXT PRIMARY KEY, value TEXT, ts REAL);
"""

TMDB_TTL = 30 * 24 * 3600   # TMDB 元数据 30 天
FEED_TTL = 3600            # 番组 feed / RSS 标题：新集的唯一信号，必须远短于调度间隔(6h)
LOOKUP_TTL = 7 * 86400     # 标题->番组 id、字幕组列表：映射关系，稳定但不是永恒
EPISODES_TTL = 6 * 3600     # 在播的季的分集表 6 小时。在播番每周新增一集，
                            # 用元数据那套 30 天会让新集整整一个月看不见
EPISODES_ENDED_TTL = 7 * 86400   # 播完的季（`episodes_ttl`）：一周问一次就够。生产 159 个季键里大半早就播完，
                                 # 以前抓取每一遍都不带缓存地重问（runloop 调研：diagnose 43.98 秒里 31.9 秒是它）
EPISODES_ENDED_AFTER_DAYS = 14   # 最后一集播出超过这么多天、又没有没播 / 没定档的集，才算播完：
                                 # 周播番断更两周是真断了；TMDB 还没把后面的集登上去时，按在播算
EPISODES_FAIL_TTL = 6 * 3600     # 分集表取不到（这一季在 TMDB 上不存在、或 TMDB 连不上）：6 小时内不再问。
                                 # 以前每个检测器、每一遍各问一次，TMDB 挂着时每个季键各等一次 20 秒超时
TMDB_MISS_TTL = 24 * 3600   # 按目录名 / AB 标题搜不到 TMDB 条目（或模型也选不出）：一天内不再搜。
                            # 以前每轮重搜库里 40 多个本来就没有条目的目录，扫描白花 8–10 秒


def episodes_ttl(eps: list[dict], today: date | None = None) -> float:
    """这份分集表能用多久：播完的季 `EPISODES_ENDED_TTL`，其余（有没播或没定档的集、最后一集播出不到
    `EPISODES_ENDED_AFTER_DAYS` 天、还没有集）`EPISODES_TTL`。"""
    today = today or date.today()
    dates = []
    for e in eps or []:
        try:
            dates.append(date.fromisoformat(str(e.get("air_date") or "")))
        except ValueError:
            return EPISODES_TTL                 # 没定档的集：还在播
    if not dates or max(dates) > today - timedelta(days=EPISODES_ENDED_AFTER_DAYS):
        return EPISODES_TTL
    return EPISODES_ENDED_TTL


def _eps_key(tmdb_id, season) -> str:
    return f"tmdbeps:{tmdb_id}:{season}"


def _eps_fail_key(tmdb_id, season) -> str:
    return f"tmdbepsfail:{tmdb_id}:{season}"


def _brief(e: BaseException) -> str:
    """取分集表失败的一句话。HTTP 错误只留状态码：httpx 的报错文本带着整个请求 URL（`api_key=` 就在里面），
    这句话要进缓存与日志。"""
    status = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"{type(e).__name__}: HTTP {status}"
    return f"{type(e).__name__}: {e}"[:200]


def _outage(e: BaseException) -> bool:
    """这次失败是不是"TMDB 连不上 / 出故障"（而不是 TMDB 答了"这一季没有"）。HTTP 4xx（429 限流除外）是这一个键的
    答案；超时、连不上、5xx、限流才是故障——同一轮里没缓存的季都别再问了。"""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return not (isinstance(status, int) and 400 <= status < 500 and status != 429)


def season_episodes(ctx, cache: "Cache", tmdb_id: int, season: int) -> tuple[list[dict] | None, str]:
    """(tmdb_id, 季) 的分集表：缓存（时效按 `episodes_ttl`）→ 负缓存（`EPISODES_FAIL_TTL`）→ 问 TMDB。
    返回 `(分集表, "")`，取不到时 `(None, 为什么)`——调用方照旧说出来、这一季这一轮不评估。

    **所有问分集表的检测器都走这里**（incomplete-season、source-abandoned、episode-available）。`run` 要迭代到
    不动点：第二次迭代起一次网络都不该打（本地动作改变不了 TMDB 的分集表）。TMDB 连不上时（`_outage`），同一个
    Context（= 一轮 `run` 的全部迭代）里没缓存的季不再问，与扫描那边"出过一次错其余不再打网络"同一个口径。
    """
    eps = cache.get_episodes(tmdb_id, season)
    if eps is not None:
        return eps, ""
    failed = cache.episodes_failure(tmdb_id, season)
    if failed:
        return None, f"{failed}（{EPISODES_FAIL_TTL // 3600} 小时内不再问）"
    down = getattr(ctx, "tmdb_episodes_down", "")
    if down:
        return None, f"这一轮 TMDB 连不上（{down}），没缓存的季不再问"
    try:
        eps = ctx.tmdb.season_episodes(tmdb_id, season)
    except Exception as e:                          # noqa: BLE001 —— 交给调用方说（每个调用方都 ctx.log 这一季没评估）
        why = _brief(e)
        cache.put_episodes_failure(tmdb_id, season, why)
        if _outage(e):
            ctx.tmdb_episodes_down = why
        return None, why
    cache.put_episodes(tmdb_id, season, eps)
    return eps, ""


class Cache:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- TMDB ---
    def get_tmdb(self, key: str, ttl: float = TMDB_TTL) -> dict | None:
        row = self.conn.execute(
            "SELECT value, ts FROM tmdb WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[1] > ttl:
            return None
        return json.loads(row[0])

    def get_tmdb_stale(self, key: str) -> dict | None:
        """不看时效的旧值：这一轮取不到新的时，拿上次的季信息 / 标题兜底（总比当作"没有"强）。"""
        row = self.conn.execute("SELECT value FROM tmdb WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_tmdb(self, key: str, value: dict) -> None:
        self.conn.execute("REPLACE INTO tmdb VALUES (?,?,?)",
                          (key, json.dumps(value, ensure_ascii=False), time.time()))
        self.conn.commit()

    # --- TMDB 分集表（`season_episodes`）---
    def get_episodes(self, tmdb_id, season) -> list[dict] | None:
        """缓存里的分集表，过了它自己的时效（`episodes_ttl`：播完的季 7 天、其余 6 小时）返回 None。"""
        row = self.conn.execute(
            "SELECT value, ts FROM tmdb WHERE key=?", (_eps_key(tmdb_id, season),)).fetchone()
        if not row:
            return None
        eps = json.loads(row[0]).get("eps")
        if not isinstance(eps, list) or time.time() - row[1] > episodes_ttl(eps):
            return None
        return eps

    def put_episodes(self, tmdb_id, season, eps: list[dict]) -> None:
        self.put_tmdb(_eps_key(tmdb_id, season), {"eps": eps})
        self.conn.execute("DELETE FROM tmdb WHERE key=?", (_eps_fail_key(tmdb_id, season),))
        self.conn.commit()

    def episodes_failure(self, tmdb_id, season) -> str:
        """`EPISODES_FAIL_TTL` 之内取失败过：返回当时的原因；否则空串。"""
        got = self.get_tmdb(_eps_fail_key(tmdb_id, season), ttl=EPISODES_FAIL_TTL)
        return str(got.get("error") or "取不到") if got is not None else ""

    def put_episodes_failure(self, tmdb_id, season, why: str) -> None:
        self.put_tmdb(_eps_fail_key(tmdb_id, season), {"error": why})

    # --- 内容哈希（按 path+size+mtime 失效）---
    def get_hash(self, path: str, size: int, mtime: float) -> str | None:
        row = self.conn.execute(
            "SELECT size, mtime, digest FROM hash WHERE path=?", (path,)).fetchone()
        if row and row[0] == size and abs(row[1] - mtime) < 1:
            return row[2]
        return None

    def put_hash(self, path: str, size: int, mtime: float, digest: str) -> None:
        self.conn.execute("REPLACE INTO hash VALUES (?,?,?,?)",
                          (path, size, mtime, digest))
        self.conn.commit()

    # --- LLM 判断 ---
    def get_llm(self, key: str, ttl: float) -> dict | None:
        """通用网络缓存。**`ttl` 是必填的，不给默认值。**

        这张表名字叫 llm，但实际一条模型判断都没存过——装的全是番组 feed、
        RSS 标题、Mikan 搜索结果这类网络数据，而它们**都是有时效的**。
        原来的实现只看"存过没有"，不看"存了多久"：

            got = cache.get_llm(ck)
            if got is None:              # 只有从没缓存过才去拉
                got = fetch(); cache.put_llm(ck, got)

        于是每部番的番组页在第一次抓取后就**永久冻结**。2026-09-03 实测：
        Re:Zero 的 feed 定格在 71 小时前那一刻，E81 已经发布 1 天而缓存里没有
        ——"谁先出要谁"的抓取模型从原理上再也看不见任何新发布，
        连续三天零抓取，且不报任何错。

        把 ttl 设成必填而不是给个默认值，是为了让"忘记考虑时效"这件事
        变成立刻抛 TypeError，而不是静默地把数据冻住。
        """
        row = self.conn.execute(
            "SELECT value, ts FROM llm WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[1] > ttl:
            return None
        return json.loads(row[0])

    def put_llm(self, key: str, value: dict) -> None:
        self.conn.execute("REPLACE INTO llm VALUES (?,?,?)",
                          (key, json.dumps(value, ensure_ascii=False), time.time()))
        self.conn.commit()
