"""抓取加种后等元数据的时长可配（`GRAB_METADATA_TIMEOUT`，默认 30 秒），等的结局写进审计。

`grabber.wait_metadata` 的默认值与模块文档一直是 30 秒（磁力 / 连不上 peer 时元数据可能要几分钟，
超时返回空、交给 `unrenamed-file` 兜底）；而 `_rename_grabbed` 实际写死 `timeout=10.0`，审计里只有
一句「元数据 10 秒内未到」——等了多久、为什么没等到（qBittorrent 报错还是真没元数据），查不到。
2026-09-03「最新三集刮削失败」就是即时改名没做成、文件在发布名上躺了六小时。
"""
from __future__ import annotations

import pytest

from media_agent import grabber
from media_agent.config import load_config
from media_agent.kernel import Action, Finding

SHOW = "躲在超市后门抽烟的两人"
TITLE = "[LoliHouse] Super no Ura de Yani Suu Futari - 12 [WebRip 1080p HEVC-10bit AAC]"


def _grab(lib):
    sh = lib.show(SHOW)
    sh.season(1)
    url, h = lib.web.torrent(TITLE)
    f = Finding(rule="episode-available", kind="episode_grabbable", severity="important",
                summary="S01E12 可抓取", show=SHOW,
                action=Action(op="grab_episode", args={
                    "url": url, "title": TITLE, "show_dir": str(sh.path), "season": 1,
                    "episode": 12, "bangumi_id": None, "category": SHOW,
                    "official_title": SHOW}))
    return f, h


def test_default_is_30_seconds_and_env_overrides(monkeypatch):
    assert load_config().grab_metadata_timeout == 30.0
    monkeypatch.setenv("GRAB_METADATA_TIMEOUT", "45")
    assert load_config().grab_metadata_timeout == 45.0


@pytest.mark.parametrize("bad", ["abc", "-1", "nan", ""])
def test_bad_value_fails_loudly(monkeypatch, bad):
    """写错了就大声失败（与 EVOLVE_MODE 同口径）：静默退回默认值会让人以为改生效了。"""
    monkeypatch.setenv("GRAB_METADATA_TIMEOUT", bad)
    with pytest.raises(ValueError, match="GRAB_METADATA_TIMEOUT"):
        load_config()


def test_grab_waits_for_the_configured_time(lib, monkeypatch):
    lib.configure(grab_metadata_timeout=12.5)
    seen = []
    real = grabber.wait_metadata

    def spy(qbit, h, timeout=30.0, interval=1.0, **kw):
        seen.append(timeout)
        return real(qbit, h, timeout=timeout, interval=interval, **kw)

    monkeypatch.setattr(grabber, "wait_metadata", spy)
    f, _ = _grab(lib)

    lib.apply([f])

    assert seen == [12.5]


def test_metadata_outcome_is_recorded_when_it_arrives(lib):
    f, _ = _grab(lib)

    rep = lib.apply([f])

    [rec] = rep.applied
    md = rec["metadata"]
    assert md["outcome"] == "ready" and md["timeout_s"] == 30.0
    assert 0 <= md["waited_s"] < 5 and "last_error" not in md
    assert rec["rename"]["renamed"] == f"{SHOW} S01E12.mkv"


def test_metadata_timeout_is_recorded_with_the_last_error(lib):
    """等满了也没拿到：记下等了多久、配置的时长、最后一次读 qBittorrent 的报错。"""
    lib.configure(grab_metadata_timeout=0.0)
    f, h = _grab(lib)
    lib.qbit.fail("files", hash=h, times=None)

    rep = lib.apply([f])

    [rec] = rep.applied                                        # 抓取本身照样成功
    md = rec["metadata"]
    assert md["outcome"] == "timeout" and md["timeout_s"] == 0.0
    assert "ReadTimeout" in md["last_error"]
    assert "0 秒" in rec["rename"]["skipped"] and rec["rename"]["renamed"] is None
