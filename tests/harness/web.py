"""FakeWeb：替换 `urllib.request.urlopen`，按 URL 路由到本地生成的响应。

本项目三条网络路径都在**调用时**查 `urllib.request.urlopen`：
`subscription._fetch_rss_titles`、`subscription._http_get`（grab.py 用
`from .subscription import _http_get` 拿的也是它）、以及 `_op_grab_episode`
下 .torrent。所以只打一个补丁就够——只补 `_http_get` 是不够的，
grab.py 绑的是自己的引用。

没配路由的 URL 抛 `URLError`（调用方把它当普通网络故障吞掉），**同时**记
tripwire：否则漏配一条路由的测试会安静地走进"拉取失败 → 跳过"分支，照样绿。
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .torrentfile import make_torrent

MIKAN = "https://mikanani.me"


def norm_url(url: str) -> str:
    """路由键：统一反转义。`_fetch_rss_titles` 会重新编码非 ASCII，
    `_mikan_search_ids` 会 quote 关键词——两边都归一成未编码形态再比。"""
    return urllib.parse.unquote(url)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, url: str = ""):
        self._body = body
        self.status = status
        self.url = url
        self.headers = {}

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self) -> None:
        pass


@dataclass
class MikanItem:
    """番组页 RSS 里的一条。`torrent` 为 None 时按 `title` 自动造一个单文件种子。"""
    title: str
    pub: str = ""                    # "YYYY-MM-DD"；空 = 不带 pubDate
    url: str = ""                    # enclosure；空则自动生成
    torrent: bytes | None = None
    infohash: str = ""


class FakeWeb:
    def __init__(self, tripwire=None):
        self.tripwire = tripwire
        self._routes: dict[str, Callable[[str], bytes] | bytes] = {}
        self._patterns: list[tuple[re.Pattern, Callable[[str], bytes] | bytes]] = []
        self.calls: list[str] = []
        self._n = 0

    # ---------------------------------------------------------------- 路由
    def route(self, url: str, body: bytes | str | Callable[[str], bytes]) -> str:
        """精确路由。`body` 可以是 bytes/str，或 `f(url) -> bytes`（可在里面 raise 模拟故障）。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._routes[norm_url(url)] = body
        return url

    def route_re(self, pattern: str, body: bytes | str | Callable[[str], bytes]) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._patterns.append((re.compile(pattern), body))

    def urlopen(self, req, data=None, timeout=None, **kw) -> FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        key = norm_url(url)
        self.calls.append(key)
        body = self._routes.get(key)
        if body is None:
            body = next((b for p, b in self._patterns if p.search(key)), None)
        if body is None:
            if self.tripwire is not None:
                self.tripwire.record("unrouted_url", key)
            raise urllib.error.URLError(f"离线测试未配置路由: {key}")
        if callable(body):
            body = body(key)
        return FakeResponse(body, url=url)

    # ---------------------------------------------------------------- 生成器
    def torrent(self, title: str, size: int = 600_000_000,
                files: dict[str, int] | None = None, url: str = "") -> tuple[str, str]:
        """登记一个可下载的 .torrent，返回 (url, infohash)。

        单文件种子的文件名 = `title` + ".mkv"（若 title 本身不带视频扩展名）。
        """
        self._n += 1
        url = url or f"{MIKAN}/Download/fake/{self._n:04d}.torrent"
        if files is None:
            fname = title if re.search(r"\.(mkv|mp4)$", title, re.I) else title + ".mkv"
            blob, h = make_torrent(fname, size=size)
        else:
            blob, h = make_torrent(title, files=files)
        self.route(url, blob)
        return url, h

    def mikan_feed(self, mikan_id: str, items: list[MikanItem]) -> str:
        """番组页全字幕组 RSS（`grab._feed_items` 读它）。每条的 .torrent 也一并登记。"""
        parts = []
        for it in items:
            if not it.url:
                it.url, it.infohash = self.torrent(it.title)
            elif it.torrent is not None:
                self.route(it.url, it.torrent)
            pub = (f"<torrent><pubDate>{it.pub}T20:00:00.00</pubDate></torrent>"
                   if it.pub else "")
            parts.append(f"<item><title>{html.escape(it.title)}</title>"
                         f'<enclosure type="application/x-bittorrent" '
                         f'url="{html.escape(it.url)}" />{pub}</item>')
        body = (f"<?xml version=\"1.0\"?><rss><channel><title>Mikan Project</title>"
                f"{''.join(parts)}</channel></rss>")
        return self.route(f"{MIKAN}/RSS/Bangumi?bangumiId={mikan_id}", body)

    def mikan_search(self, keyword: str, ids: list[str]) -> str:
        """`/Home/Search?searchstr=`：返回指向这些番组 id 的链接。"""
        links = "".join(f'<a href="/Home/Bangumi/{i}">x</a>' for i in ids)
        return self.route(
            f"{MIKAN}/Home/Search?searchstr={urllib.parse.quote(keyword)}",
            f"<html><body>{links}</body></html>")

    def mikan_subgroups(self, mikan_id: str, groups: dict[str, str]) -> str:
        """`/Home/Bangumi/<id>`：`{subgroupid: 字幕组名}`。"""
        divs = "".join(f'<div class="subgroup-text" id="{g}">{html.escape(n)}</div>'
                       for g, n in groups.items())
        return self.route(f"{MIKAN}/Home/Bangumi/{mikan_id}", f"<html>{divs}</html>")

    def rss(self, url: str, titles: list[str]) -> str:
        """任意 RSS（AutoBangumi 的 rss_link 等）：只有 `<item><title>`。"""
        items = "".join(f"<item><title>{html.escape(t)}</title></item>" for t in titles)
        return self.route(url, f"<rss><channel><title>ch</title>{items}</channel></rss>")


def days_ago(n: int) -> str:
    """相对今天的 ISO 日期。代码里直接调 `date.today()`，所以测试数据一律用相对日期。"""
    return (date.today() - timedelta(days=n)).isoformat()
