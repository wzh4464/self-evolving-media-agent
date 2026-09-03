"""磁盘缓存：TMDB 结果、内容哈希、LLM 判断。避免每轮重复付费/重复 IO。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tmdb (key TEXT PRIMARY KEY, value TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS hash (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, digest TEXT);
CREATE TABLE IF NOT EXISTS llm (key TEXT PRIMARY KEY, value TEXT, ts REAL);
"""

TMDB_TTL = 30 * 24 * 3600   # TMDB 元数据 30 天
FEED_TTL = 3600            # 番组 feed / RSS 标题：新集的唯一信号，必须远短于调度间隔(6h)
LOOKUP_TTL = 7 * 86400     # 标题->番组 id、字幕组列表：映射关系，稳定但不是永恒
EPISODES_TTL = 6 * 3600     # 分集表 6 小时。在播番每周新增一集，
                            # 用元数据那套 30 天会让新集整整一个月看不见


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

    def put_tmdb(self, key: str, value: dict) -> None:
        self.conn.execute("REPLACE INTO tmdb VALUES (?,?,?)",
                          (key, json.dumps(value, ensure_ascii=False), time.time()))
        self.conn.commit()

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
