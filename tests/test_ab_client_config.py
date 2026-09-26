"""AutoBangumi 的配置 / 程序接口：`ab-mode` 切换 AB 的两个开关要用到（ab 调研 §3.2，AB 3.2.6 源码只读核对）。

- `GET /api/v1/config/get`：整份配置，密码类的值打成 `********`；
- `PATCH /api/v1/config/update`：**整份**配置对象（按 pydantic 的 `Config` 整体解析，漏掉的段退回默认值——
  `downloader.host` 变成 `172.17.0.1:8080`、`rename_method` 变成 `pn`），打码的值服务端按现值还原；
- `GET /api/v1/restart`：`Program.restart()`，停掉四个后台任务再按 config.json 起开着的——开关只在这时生效；
- `GET /api/v1/status`：`_tasks_started` 为真才是 `status: true`（重启中途是 false）。

真客户端的线路形状用 httpx 的 MockTransport 钉住（`restart` 是 GET、`update` 是 PATCH 带整份 JSON）；FakeAB 按 AB 的语义
建模这几个接口，供切换的测试用。
"""
from __future__ import annotations

import json

import httpx
import pytest
from harness import FakeAB
from harness.ab import AB_DEFAULT_CONFIG

from media_agent.clients import AutoBangumiClient


def _client(handler) -> tuple[AutoBangumiClient, list]:
    seen: list = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    c = object.__new__(AutoBangumiClient)          # 不走构造函数：它会真的登录
    c.base = "http://ab.invalid"
    c.token = "t"
    c._client = httpx.Client(transport=httpx.MockTransport(record))
    return c, seen


def test_the_client_reads_patches_restarts_and_polls_with_the_right_verbs():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/config/get":
            return httpx.Response(200, json={"rss_parser": {"enable": True}})
        if req.url.path == "/api/v1/config/update":
            return httpx.Response(200, json={"msg_en": "Update config successfully."})
        if req.url.path == "/api/v1/restart":
            return httpx.Response(200, json={"status": True, "msg_en": "Program restarted."})
        if req.url.path == "/api/v1/status":
            return httpx.Response(200, json={"status": True, "version": "3.2.6", "first_run": False})
        return httpx.Response(404)

    c, seen = _client(handler)
    assert c.get_config() == {"rss_parser": {"enable": True}}
    full = {"rss_parser": {"enable": False}, "bangumi_manage": {"enable": False}, "program": {"rss_time": 900}}
    c.update_config(full)
    c.restart()
    assert c.status()["status"] is True

    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/config/get"), ("PATCH", "/api/v1/config/update"),
        ("GET", "/api/v1/restart"), ("GET", "/api/v1/status")]
    assert json.loads(seen[1].content) == full        # 整份原样发回，不是只发改的那两项


def test_a_refused_update_raises():
    """AB 写不进配置时回 406（`api/config.py`）：不能当成功。"""
    c, _ = _client(lambda req: httpx.Response(406, json={"msg_en": "Update config failed."}))
    with pytest.raises(httpx.HTTPStatusError):
        c.update_config({"rss_parser": {"enable": False}})


# ---------------------------------------------------------------- FakeAB 的语义
def test_fake_masks_secrets_and_restores_them_on_update():
    ab = FakeAB()
    got = ab.get_config()
    assert got["downloader"]["password"] == "********"
    got["rss_parser"]["enable"] = False
    ab.update_config(got)
    assert ab.config["downloader"]["password"] == ab.SECRET          # 打码的值按现值还原
    assert ab.config["rss_parser"]["enable"] is False


def test_fake_resets_omitted_sections_to_defaults():
    """PATCH 只发改的那一段 = 其余各段退回默认值：这正是必须发回整份的原因。"""
    ab = FakeAB()
    ab.update_config({"rss_parser": {"enable": False}})
    assert ab.config["downloader"]["host"] == AB_DEFAULT_CONFIG["downloader"]["host"] != ab.HOST
    assert ab.config["bangumi_manage"]["rename_method"] == "pn"


def test_fake_flags_only_take_effect_at_restart():
    ab = FakeAB()
    cfg = ab.get_config()
    cfg["rss_parser"]["enable"] = cfg["bangumi_manage"]["enable"] = False
    ab.update_config(cfg)
    assert ab.running == {"rss": True, "renamer": True}           # 线程从不回头看开关
    ab.restart()
    assert ab.running == {"rss": False, "renamer": False}
    assert ab.status()["status"] is True


def test_fake_restart_can_be_slow():
    """`Program.start()` 先等下载器（最多 10 × 30 秒）：重启请求超时、之后几次 status 还是 false。"""
    ab = FakeAB()
    ab.slow_restart(polls=2)
    with pytest.raises(httpx.ReadTimeout):
        ab.restart()
    assert [ab.status()["status"] for _ in range(3)] == [False, False, True]
