"""AutoBangumi 的两个替身。

- `FakeAB`：REST 客户端。本项目只调 `all_bangumi()`（`_wait_ab_ready` 探活）
  和 `refresh_all()`；`subscribe` / `refresh` 没人调，调了就报 tripwire。
- **库用真的**：`make_ab_db()` 在临时目录里按生产 DDL 建一个 sqlite，
  交给真实的 `AutoBangumiDB`。这样 `mode=ro` 的 URI、真 SQL、以及
  "停容器 → 改库 → 起容器" 的顺序都被执行到；docker 换成一个只记参数的
  shell 脚本（`docker_log` 里能看到 `stop autobangumi` / `start autobangumi`）。

DDL 取自生产库 2026-09-26 的只读 schema 导出，只保留代码读写到的表。
"""
from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

from media_agent.clients import AutoBangumiDB

AB_DDL = """
CREATE TABLE bangumi (id INTEGER PRIMARY KEY, official_title VARCHAR NOT NULL, year VARCHAR,
  title_raw VARCHAR NOT NULL, season INTEGER NOT NULL DEFAULT 1, season_raw VARCHAR,
  group_name VARCHAR, dpi VARCHAR, source VARCHAR, subtitle VARCHAR,
  eps_collect BOOLEAN NOT NULL DEFAULT 0, episode_offset INTEGER NOT NULL DEFAULT 0,
  season_offset INTEGER NOT NULL DEFAULT 0, filter VARCHAR NOT NULL DEFAULT '',
  rss_link VARCHAR NOT NULL DEFAULT '', poster_link VARCHAR, added BOOLEAN NOT NULL DEFAULT 1,
  rule_name VARCHAR, save_path VARCHAR, deleted BOOLEAN NOT NULL DEFAULT 0,
  archived BOOLEAN NOT NULL DEFAULT 0, air_weekday INTEGER,
  weekday_locked BOOLEAN NOT NULL DEFAULT 0, needs_review BOOLEAN NOT NULL DEFAULT 0,
  needs_review_reason VARCHAR, suggested_season_offset INTEGER,
  suggested_episode_offset INTEGER, title_aliases VARCHAR);
CREATE TABLE rssitem (id INTEGER PRIMARY KEY, name VARCHAR, url VARCHAR NOT NULL,
  aggregate BOOLEAN NOT NULL DEFAULT 0, parser VARCHAR NOT NULL DEFAULT 'mikan',
  enabled BOOLEAN NOT NULL DEFAULT 1, connection_status VARCHAR, last_checked_at VARCHAR,
  last_error VARCHAR);
CREATE TABLE torrent (id INTEGER PRIMARY KEY, bangumi_id INTEGER REFERENCES bangumi(id),
  rss_id INTEGER REFERENCES rssitem(id), name VARCHAR NOT NULL, url VARCHAR NOT NULL DEFAULT '',
  homepage VARCHAR, downloaded BOOLEAN NOT NULL DEFAULT 0, qb_hash VARCHAR);
"""


def make_docker_stub(root: Path) -> tuple[Path, Path]:
    """返回 (stub 可执行文件, 日志文件)。stub 把每次调用的参数追加进日志。

    同目录下有 `fail-<子命令>` 文件时，那个子命令记完日志后以 1 退出（`LibraryBuilder.docker_fail`）：
    `docker start` 失败就是"库已改好、容器没起来"的现场。"""
    root.mkdir(parents=True, exist_ok=True)
    log = root / "docker.log"
    stub = root / "docker"
    stub.write_text('#!/bin/sh\necho "$@" >> "%s"\n'
                    'if [ -e "%s/fail-$1" ]; then echo "injected docker $1 failure" >&2; exit 1; fi\n'
                    % (log, root), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub, log


def make_ab_db(root: Path, container: str = "autobangumi") -> tuple[AutoBangumiDB, Path]:
    """临时 AB 库 + docker 替身。返回 (真实的 AutoBangumiDB, docker 日志路径)。"""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "data.db"
    conn = sqlite3.connect(db)
    conn.executescript(AB_DDL)
    conn.commit()
    conn.close()
    stub, log = make_docker_stub(root)
    return AutoBangumiDB(str(db), container, str(stub)), log


def insert_bangumi(abdb: AutoBangumiDB, *, id: int, official_title: str, title_raw: str,
                   save_path: str = "", season: int = 1, group_name: str = "",
                   rss_link: str = "", episode_offset: int = 0,
                   aliases: list[str] | None = None, deleted: bool = False) -> dict:
    """直接写一行 bangumi（绕过 docker：这是布置测试现场，不是被测动作）。"""
    conn = sqlite3.connect(abdb.db_path)
    conn.execute(
        "INSERT INTO bangumi (id, official_title, title_raw, season, group_name, rss_link,"
        " episode_offset, save_path, deleted, title_aliases) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (id, official_title, title_raw, season, group_name, rss_link, episode_offset,
         save_path, int(deleted), json.dumps(aliases or [], ensure_ascii=False)))
    conn.commit()
    conn.close()
    return abdb.query("SELECT * FROM bangumi WHERE id=?", (id,))[0]


def insert_rows(abdb: AutoBangumiDB, table: str, rows: list[dict]) -> None:
    """往 rssitem / torrent 表塞行。"""
    conn = sqlite3.connect(abdb.db_path)
    for r in rows:
        cols = ",".join(r)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(r))})",
                     tuple(r.values()))
    conn.commit()
    conn.close()


class FakeAB:
    """AutoBangumi REST。`fail("all_bangumi", times=2)` 可模拟容器刚起、端口还没监听。"""

    def __init__(self, tripwire=None, abdb: AutoBangumiDB | None = None):
        self.tripwire = tripwire
        self.abdb = abdb
        self.calls: list[str] = []
        self._fail: dict[str, list] = {}

    def fail(self, method: str, times: int = 1, exc: BaseException | None = None) -> None:
        self._fail[method] = [times, exc or ConnectionResetError("Connection reset by peer")]

    def _maybe_fail(self, method: str) -> None:
        f = self._fail.get(method)
        if f and f[0] > 0:
            f[0] -= 1
            raise f[1]

    def all_bangumi(self) -> list[dict]:
        self.calls.append("all_bangumi")
        self._maybe_fail("all_bangumi")
        return self.abdb.bangumi() if self.abdb else []

    def refresh_all(self) -> dict:
        self.calls.append("refresh_all")
        self._maybe_fail("refresh_all")
        return {"status": True, "msg_en": "Refresh all RSS successfully."}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if self.tripwire is not None:
            self.tripwire.record("unmodeled", f"FakeAB.{name}")
        raise AttributeError(f"FakeAB.{name} 未建模")
