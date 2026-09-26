"""AutoBangumi 的两个替身。

- `FakeAB`：REST 客户端。本项目调 `all_bangumi()`（`_wait_ab_ready` 探活）、`refresh_all()`，以及
  `media-agent ab-mode` 用的配置 / 程序接口（`get_config` / `update_config` / `restart` / `status`，按 AB 3.2.6 的语义
  建模：读出来的密码打码、写回时按现值还原；PATCH 漏掉的段退回默认值；两个开关只在重启时生效）；
  `subscribe` / `refresh` 没人调，调了就报 tripwire。
- **库用真的**：`make_ab_db()` 在临时目录里按生产 DDL 建一个 sqlite，
  交给真实的 `AutoBangumiDB`。这样 `mode=ro` 的 URI、真 SQL、以及
  "停容器 → 改库 → 起容器" 的顺序都被执行到；docker 换成一个只记参数的
  shell 脚本（`docker_log` 里能看到 `stop autobangumi` / `start autobangumi`）。

DDL 取自生产库 2026-09-26 的只读 schema 导出，只保留代码读写到的表。
"""
from __future__ import annotations

import copy
import json
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import httpx

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
                   rss_link: str = "", episode_offset: int = 0, season_offset: int = 0,
                   aliases: list[str] | None = None, deleted: bool = False) -> dict:
    """直接写一行 bangumi（绕过 docker：这是布置测试现场，不是被测动作）。"""
    conn = sqlite3.connect(abdb.db_path)
    conn.execute(
        "INSERT INTO bangumi (id, official_title, title_raw, season, group_name, rss_link,"
        " episode_offset, season_offset, save_path, deleted, title_aliases)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (id, official_title, title_raw, season, group_name, rss_link, episode_offset,
         season_offset, save_path, int(deleted), json.dumps(aliases or [], ensure_ascii=False)))
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


# AB 3.2.6 `models/config.py` 的默认值（本项目碰得到的几段）。`update_config` 收到的对象里漏掉的段按它重置——
# pydantic 整体解析 `Config` 的行为（ab 调研 §3.2 的警告）
AB_DEFAULT_CONFIG = {
    "program": {"rss_time": 900, "rename_time": 60, "webui_port": 7892},
    "downloader": {"type": "qbittorrent", "host": "172.17.0.1:8080", "username": "admin",
                   "password": "adminadmin", "path": "/downloads/Bangumi", "ssl": False},
    "rss_parser": {"enable": True, "filter": ["720", "\\d+-\\d+"], "language": "zh"},
    "bangumi_manage": {"enable": True, "eps_complete": False, "rename_method": "pn", "group_tag": False,
                       "remove_bad_torrent": False},
    "log": {"debug_enable": False},
    "proxy": {"enable": False, "type": "http", "host": "", "port": 0, "username": "", "password": ""},
    "notification": {"enable": False, "type": "telegram", "token": "", "chat_id": ""},
    "security": {"login_whitelist": [], "login_tokens": [], "mcp_whitelist": [], "mcp_tokens": []},
}
_MASK = "********"
_SENSITIVE = ("password", "api_key", "token", "secret")


def _mask(d: dict) -> dict:
    """AB 的 `_sanitize_dict`：键名含 password / api_key / token / secret 的**字符串**值打码。"""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _mask(v)
        elif isinstance(v, str) and any(s in k.lower() for s in _SENSITIVE):
            out[k] = _MASK
        else:
            out[k] = copy.deepcopy(v)
    return out


def _unmask(incoming: dict, current: dict) -> dict:
    """AB 的 `_restore_masked`：打码的值按现值还原。"""
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            _unmask(v, current[k])
        elif v == _MASK and any(s in k.lower() for s in _SENSITIVE):
            incoming[k] = current.get(k, v)
    return incoming


class FakeAB:
    """AutoBangumi REST。`fail("all_bangumi", times=2)` 可模拟容器刚起、端口还没监听。

    配置与程序（`media-agent ab-mode`）：`config` 是 config.json 里此刻存的（生产的形状：下载器指向宿主、改名 `advance`、
    密码是 `SECRET`）；`running` 是此刻真在跑的两个线程——`update_config` 不动它，`restart()` 才按 `config` 重设
    （AB 的循环从不回头看开关）。`slow_restart(polls=n)`：重启请求超时，之后 n 次 `status()` 是 false；`hang_restart()`：
    一直起不来。`poll_rss()`：RSS 线程在跑时，把启用的 rssitem 的 `last_checked_at` 推到此刻（AB 每 15 分钟做的事）。
    """

    SECRET = "not-a-real-password-0000"
    HOST = "qbit.invalid:8080"

    def __init__(self, tripwire=None, abdb: AutoBangumiDB | None = None):
        self.tripwire = tripwire
        self.abdb = abdb
        self.calls: list[str] = []
        self._fail: dict[str, list] = {}
        self.config = copy.deepcopy(AB_DEFAULT_CONFIG)
        self.config["downloader"].update(host=self.HOST, username="ab", password=self.SECRET,
                                         path="/Volumes/Media")
        self.config["bangumi_manage"]["rename_method"] = "advance"
        self.running = {"rss": True, "renamer": True}
        self.up = True
        self._restart_polls: int | None = 0         # 重启之后还要几次 status() 才回来；None = 永远不回来
        self._restart_exc: BaseException | None = None
        self._pending_polls = 0

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

    # ---- 配置与程序 ----
    def flags(self) -> dict:
        """config.json 里此刻的两个开关。"""
        return {"rss_parser.enable": self.config["rss_parser"]["enable"],
                "bangumi_manage.enable": self.config["bangumi_manage"]["enable"]}

    def get_config(self) -> dict:
        self.calls.append("get_config")
        self._maybe_fail("get_config")
        return _mask(self.config)

    def update_config(self, config: dict) -> dict:
        self.calls.append("update_config")
        self._maybe_fail("update_config")
        incoming = copy.deepcopy(config)
        new = {k: copy.deepcopy(incoming.get(k, v)) for k, v in AB_DEFAULT_CONFIG.items()}
        for k, v in incoming.items():
            new.setdefault(k, v)
        self.config = _unmask(new, self.config)
        return {"msg_en": "Update config successfully.", "msg_zh": "更新配置成功。"}

    def slow_restart(self, polls: int, exc: BaseException | None = None) -> None:
        self._restart_polls = polls
        self._restart_exc = exc or httpx.ReadTimeout("timed out")

    def hang_restart(self) -> None:
        self._restart_polls = None
        self._restart_exc = httpx.ReadTimeout("timed out")

    def _start(self) -> None:
        self.running = {"rss": bool(self.config["rss_parser"]["enable"]),
                        "renamer": bool(self.config["bangumi_manage"]["enable"])}
        self.up = True

    def restart(self, timeout: float = 30.0) -> dict:
        self.calls.append("restart")
        self._maybe_fail("restart")
        self.running = {"rss": False, "renamer": False}
        self.up = False
        if self._restart_polls == 0:
            self._start()
            return {"status": True, "msg_en": "Program restarted."}
        self._pending_polls = self._restart_polls
        raise self._restart_exc

    def status(self) -> dict:
        self.calls.append("status")
        self._maybe_fail("status")
        if not self.up and self._pending_polls is not None:
            if self._pending_polls <= 0:
                self._start()
            else:
                self._pending_polls -= 1
        return {"status": self.up, "version": "3.2.6", "first_run": False}

    def poll_rss(self, at: str | None = None) -> bool:
        """RSS 线程在跑就拉一遍：启用的 rssitem 的 `last_checked_at` 推到 `at`（默认此刻，AB 的 UTC ISO 格式）。"""
        if not (self.running["rss"] and self.abdb):
            return False
        at = at or datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.abdb.db_path)
        conn.execute("UPDATE rssitem SET last_checked_at=? WHERE enabled=1", (at,))
        conn.commit()
        conn.close()
        return True

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if self.tripwire is not None:
            self.tripwire.record("unmodeled", f"FakeAB.{name}")
        raise AttributeError(f"FakeAB.{name} 未建模")
