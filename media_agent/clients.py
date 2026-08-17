"""外部服务客户端。每个类的注释都标注了实测踩过的坑。"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from typing import Any

import httpx


class QBitError(RuntimeError):
    pass


class QBitClient:
    """qBittorrent WebUI API。

    坑位记录：
    - 新版登录成功返回 `204 No Content` + 空 body，旧版是 `200` + `"Ok."`。
      AutoBangumi 3.2.6 只认后者，导致"Auth failed"死循环——这里两种都认。
    - `renameFile` 只改磁盘 content_path，**不改** torrent 的 `name` 字段，
      所以判断"是否已改名"必须查磁盘或 content_path，不能查 name。
    - `setCategory` 到不存在的分类会静默失败，必须先 `createCategory`。
    - `filePrio` 的 id 参数用 `|` 分隔，用 `,` 会 400。
    """

    def __init__(self, base: str, user: str, password: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Referer": self.base})
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        r = self._client.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": user, "password": password},
        )
        ok = (r.status_code == 200 and r.text.strip() == "Ok.") or r.status_code == 204
        if not ok:
            raise QBitError(f"qBittorrent 登录失败: HTTP {r.status_code} {r.text[:100]!r}")

    def _post(self, path: str, data: dict | None = None) -> httpx.Response:
        r = self._client.post(f"{self.base}/api/v2/{path}", data=data or {})
        if r.status_code >= 400:
            raise QBitError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self._client.get(f"{self.base}/api/v2/{path}", params=params or {})
        if r.status_code >= 400:
            raise QBitError(f"{path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.text.strip() else None

    # --- 查询 ---
    def torrents(self, category: str | None = None) -> list[dict]:
        params = {"category": category} if category else {}
        return self._get("torrents/info", params) or []

    def files(self, torrent_hash: str) -> list[dict]:
        return self._get("torrents/files", {"hash": torrent_hash}) or []

    def trackers(self, torrent_hash: str) -> list[dict]:
        return self._get("torrents/trackers", {"hash": torrent_hash}) or []

    def categories(self) -> dict:
        return self._get("torrents/categories") or {}

    # --- 变更 ---
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self._post("torrents/renameFile",
                   {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path})

    def set_location(self, hashes: list[str], location: str) -> None:
        self._post("torrents/setLocation",
                   {"hashes": "|".join(hashes), "location": location})

    def create_category(self, name: str, save_path: str = "") -> None:
        try:
            self._post("torrents/createCategory",
                       {"category": name, "savePath": save_path})
        except QBitError:
            pass  # 已存在

    def set_category(self, hashes: list[str], category: str) -> None:
        self.create_category(category)
        self._post("torrents/setCategory",
                   {"hashes": "|".join(hashes), "category": category})

    def remove_categories(self, categories: list[str]) -> None:
        """删除分类定义。分类下若还有种子，它们会变成"无分类"而非被删除。"""
        self._post("torrents/removeCategories",
                   {"categories": "\n".join(categories)})   # 注意是换行分隔

    def add_tags(self, hashes: list[str], tags: str) -> None:
        self._post("torrents/addTags", {"hashes": "|".join(hashes), "tags": tags})

    def set_file_priority(self, torrent_hash: str, ids: list[int], priority: int) -> None:
        self._post("torrents/filePrio", {
            "hash": torrent_hash,
            "id": "|".join(str(i) for i in ids),   # 必须是 | 不是 ,
            "priority": priority,
        })

    def delete(self, hashes: list[str], delete_files: bool) -> None:
        self._post("torrents/delete", {
            "hashes": "|".join(hashes),
            "deleteFiles": "true" if delete_files else "false",
        })


class AutoBangumiClient:
    """AutoBangumi REST API。

    坑位记录：
    - `/api/v1/rss/add` 只登记 RSS 监控，**不创建 bangumi 记录也不触发下载**，
      看起来成功实则空转。真正订阅必须用 `/api/v1/rss/subscribe`。
    - `/api/v1/rss/refresh/{id}` 是 **GET**，用 POST 会 405。
    - `/api/v1/rss/delete/{id}` 有 FK 约束 bug，产生过 torrent 记录就删不掉，
      只能停容器后直接改 sqlite。
    - 只有经它自己的下载器加的种子才会带 `ab:<id>` 标签，
      手动往 qBittorrent 加种子 = 永久脱离它的自动改名管辖。
    """

    def __init__(self, base: str, user: str, password: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        r = self._client.post(
            f"{self.base}/api/v1/auth/login",
            data={"username": user, "password": password},
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self._client.headers["Cookie"] = f"token={self.token}"

    def all_bangumi(self) -> list[dict]:
        r = self._client.get(f"{self.base}/api/v1/bangumi/get/all")
        r.raise_for_status()
        return r.json()

    def subscribe(self, bangumi: dict, rss: dict) -> dict:
        """真正的订阅入口——会创建 bangumi 记录并触发下载引擎。"""
        r = self._client.post(f"{self.base}/api/v1/rss/subscribe",
                              json={"data": bangumi, "rss": rss})
        r.raise_for_status()
        return r.json()

    def refresh(self, rss_id: int) -> dict:
        r = self._client.get(f"{self.base}/api/v1/rss/refresh/{rss_id}")  # GET!
        r.raise_for_status()
        return r.json()

    def refresh_all(self) -> dict:
        r = self._client.get(f"{self.base}/api/v1/rss/refresh/all")
        r.raise_for_status()
        return r.json()


class AutoBangumiDB:
    """直接读 AutoBangumi 的 sqlite。写操作需先停容器（应用层持有连接）。"""

    def __init__(self, db_path: str, container: str, docker_bin: str):
        self.db_path = str(db_path)
        self.container = container
        self.docker_bin = docker_bin

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def bangumi(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM bangumi"
        if not include_deleted:
            sql += " WHERE deleted=0"
        return self.query(sql)

    def rss_items(self) -> list[dict]:
        return self.query("SELECT * FROM rssitem")

    def write(self, statements: list[tuple[str, tuple]]) -> None:
        """停容器 → 改库 → 起容器。用于 API 改不了的字段（save_path/rss_link）。"""
        subprocess.run([self.docker_bin, "stop", self.container],
                       check=True, capture_output=True)
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                for sql, params in statements:
                    conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()
        finally:
            subprocess.run([self.docker_bin, "start", self.container],
                           check=True, capture_output=True)


class TMDBClient:
    """TMDB —— 目录名/季结构的唯一权威来源。"""

    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, lang: str = "zh-CN", timeout: float = 20.0):
        self.api_key = api_key
        self.lang = lang
        self._client = httpx.Client(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, **params) -> dict:
        params.update({"api_key": self.api_key, "language": self.lang})
        r = self._client.get(f"{self.BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def search_tv(self, query: str) -> list[dict]:
        return self._get("/search/tv", query=query).get("results", [])

    def tv_detail(self, tv_id: int) -> dict:
        return self._get(f"/tv/{tv_id}")

    def official_title(self, tv_id: int) -> tuple[str, str]:
        """返回 (本地化标题, 原始标题)。本地化为空时回退到原始标题。"""
        d = self.tv_detail(tv_id)
        local = (d.get("name") or "").strip()
        original = (d.get("original_name") or "").strip()
        return (local or original), original

    def seasons(self, tv_id: int) -> list[dict]:
        """返回 [{season_number, episode_count, name}]，已过滤 specials 之外的空季。"""
        d = self.tv_detail(tv_id)
        return [
            {"season_number": s["season_number"],
             "episode_count": s.get("episode_count", 0),
             "name": s.get("name", "")}
            for s in d.get("seasons", [])
        ]


class AniListClient:
    """AniList —— 订阅前校验季数/播出状态，防止抓到上一季重播。

    坑位：不带 User-Agent 会 403。
    """

    URL = "https://graphql.anilist.co"
    QUERY = """
    query ($search: String) {
      Page(perPage: 6) {
        media(search: $search, type: ANIME, sort: POPULARITY_DESC) {
          id title { romaji english native }
          format status season seasonYear episodes
          startDate { year month day }
        }
      }
    }"""

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "media-agent/0.1", "Accept": "application/json"},
        )

    def search(self, title: str, retries: int = 3) -> list[dict]:
        for attempt in range(retries):
            try:
                r = self._client.post(
                    self.URL, json={"query": self.QUERY, "variables": {"search": title}}
                )
                r.raise_for_status()
                return r.json()["data"]["Page"]["media"]
            except Exception:
                if attempt == retries - 1:
                    return []
                time.sleep(2 * (attempt + 1))   # AniList 限流，退避重试
        return []


class LLMClient:
    """deepseek (openlux)，用于确定性规则覆盖不到的模糊判断。"""

    def __init__(self, base: str, key: str, model: str, timeout: float = 120.0):
        self.base = base.rstrip("/")
        self.key = key
        self.model = model
        self._client = httpx.Client(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def ask_json(self, system: str, user: str, retries: int = 2,
                 on_error=None) -> dict | None:
        """要求模型返回 JSON。解析失败返回 None，调用方须能降级。

        兼容模型把 JSON 包在 ```json 围栏里返回的情况。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        last_err = ""
        for attempt in range(retries + 1):
            try:
                r = self._client.post(
                    f"{self.base}/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.key}"},
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
                content = content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                return json.loads(content)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt == retries:
                    if on_error:
                        on_error(last_err)
                    return None
                time.sleep(2 * (attempt + 1))
        return None
