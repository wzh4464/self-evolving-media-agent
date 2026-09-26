"""qBittorrent 的 HTTP 错误按状态码认，不按错误文本里有没有 "404" 认。

"种子已不在"（`torrents/files` 404）在执行器与占用索引里是"状态变了"：改名记 skipped、
占用查询当它不声明任何东西（testinfra B2）。以前三处各自写 `"404" in str(e)`：
错误文本里只要碰巧出现 404 这三个字符（响应体里的一段 hash、端口号、别的报错），
一次读失败就被当成"种子已经没了"——占用索引把它当空、改名把它当已删跳过，
本该 fail closed 的地方放行了。`QBitError.status` 是 HTTP 状态码本身。
"""
from __future__ import annotations

import httpx
import pytest

from harness import video
from media_agent.clients import QBitClient, QBitError, is_not_found
from media_agent.plugins.builtin import UnrenamedDetector

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"


def _client(handler) -> QBitClient:
    """不登录、不联网的真 QBitClient：请求交给 `httpx.MockTransport`。"""
    c = QBitClient.__new__(QBitClient)
    c.base = "http://qbit.invalid"
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_real_client_errors_carry_the_http_status_and_keep_their_text():
    c = _client(lambda req: httpx.Response(404, text="Not Found"))
    with pytest.raises(QBitError) as ei:
        c.files("f" * 40)
    assert ei.value.status == 404
    assert str(ei.value) == "torrents/files -> HTTP 404: Not Found"     # 文本与以前一字不差

    c = _client(lambda req: httpx.Response(409, text="conflict"))
    with pytest.raises(QBitError) as ei:
        c.rename_file("f" * 40, "a.mkv", "b.mkv")
    assert ei.value.status == 409

    c = _client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(QBitError) as ei:
        c.add_torrent(b"d4:infod4:name1:xee")
    assert ei.value.status == 500
    assert str(ei.value) == "torrents/add -> HTTP 500: boom"


def test_login_failure_carries_the_status():
    c = QBitClient.__new__(QBitClient)
    c.base = "http://qbit.invalid"
    c._client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(403, text="Forbidden")))
    with pytest.raises(QBitError) as ei:
        c._login("u", "p")
    assert ei.value.status == 403


def test_is_not_found_reads_the_status_not_the_text():
    assert is_not_found(QBitError("torrents/files -> HTTP 404: Not Found", status=404))
    # 文本里碰巧有 404，状态码不是：不算
    assert not is_not_found(QBitError("torrents/files -> HTTP 500: at ab404f", status=500))
    assert not is_not_found(httpx.ReadTimeout("timed out after 404 ms"))
    # 没有状态码的旧式构造（第三方代码 / 老测试）：不猜
    assert not is_not_found(QBitError("torrents/files -> HTTP 404: Not Found"))


def test_fake_qbit_404_has_the_same_status(lib, tripwire):
    with pytest.raises(QBitError) as ei:
        lib.qbit.files("f" * 40)
    assert ei.value.status == 404
    tripwire.allow("qbit_error")


@pytest.mark.allow("failed_record", match="无法确认目标路径的占用情况")
def test_a_read_error_whose_text_mentions_404_is_not_taken_as_torrent_gone(lib):
    """`files()` 报 HTTP 500、响应体里碰巧有 "404"：这是读不到，不是种子没了。

    以前占用索引把它当 404、认作"它不声明任何东西"放行，改名接着再问一次 `files()`，
    又按 404 记成「所属种子已不在」跳过——两处都把一次读失败当成了状态变化。
    现在占用查询看不全，这一条记 failed、什么都不动。
    """
    s1 = lib.show("尼古喵喵").season(1)
    t = s1.single(LOLI_08, size=593_601_176, probe=video("hevc"))
    findings = lib.diagnose(detectors=[UnrenamedDetector])
    lib.qbit.fail("files", hash=t.hash, times=None,
                  exc=QBitError("torrents/files -> HTTP 500: upstream ab404f", status=500))

    rep = lib.apply(findings)

    assert not rep.skipped
    [fail] = rep.failed
    assert "无法确认" in fail["error"]
    assert lib.qbit.file_names(t.hash) == [LOLI_08]                    # 一个字没改
