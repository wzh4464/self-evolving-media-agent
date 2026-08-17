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


class Cache:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # --- TMDB ---
    def get_tmdb(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT value, ts FROM tmdb WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[1] > TMDB_TTL:
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
    def get_llm(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT value FROM llm WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_llm(self, key: str, value: dict) -> None:
        self.conn.execute("REPLACE INTO llm VALUES (?,?,?)",
                          (key, json.dumps(value, ensure_ascii=False), time.time()))
        self.conn.commit()
