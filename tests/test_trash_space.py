"""搬进隔离区（和从隔离区搬回去）之前，先看目标卷放不放得下。

隔离区（`state/trash`，系统数据卷）与媒体（`/Volumes/Backup`）是同一个 APFS 容器里的两个卷
（critic N8）：`shutil.move` 跨卷就是**先拷后删**，拷的那一刻要多占一整份文件的空间；容器约 94% 满。
拷到一半 ENOSPC：`_op_trash` 那时种子那一步已经做了（整种子摘掉或设为不下载），文件却还在原处，
隔离区里还留下半个拷贝；回退从隔离区搬回时同样会在媒体库里留下一个截断的文件——下一轮扫描把它
当成一集。所以动手之前先量，放不下就跳过、写明理由，种子与文件都不动。
"""
from __future__ import annotations

from harness import video
from media_agent import disposal

GB = 600_000_000
CHI = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
RAW = video("h264")


def _scene(lib):
    s1 = lib.show("尼古喵喵").season(1)
    good = s1.single("[LoliHouse] Yani Neko - 05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
                     size=GB - 7, probe=CHI)
    raw = s1.single("[Raw] Yani Neko - 05 (1080p AVC).mkv", size=GB + 100_000_000, probe=RAW)
    return s1, good, raw


def test_trash_is_skipped_when_the_copy_cannot_fit(lib, monkeypatch):
    s1, good, raw = _scene(lib)
    monkeypatch.setattr(disposal, "free_bytes", lambda path: GB + 100_000_000)   # 刚好一份，没余量

    c = lib.cycle()

    assert not c.applied("trash")
    [rec] = c.skipped("trash")
    assert "放不下" in rec["reason"]
    assert rec["deletion"]["gate"] == "passed"            # 关口过了，是空间不够
    assert raw.path.exists() and lib.qbit.has(raw.hash)   # 种子与文件都不动
    assert lib.trash_files() == []


def test_trash_is_skipped_when_free_space_cannot_be_read(lib, monkeypatch):
    s1, good, raw = _scene(lib)
    monkeypatch.setattr(disposal, "free_bytes", lambda path: None)

    c = lib.cycle()

    [rec] = c.skipped("trash")
    assert "剩余空间" in rec["reason"]
    assert raw.path.exists() and lib.qbit.has(raw.hash)


def test_trash_proceeds_with_room_to_spare(lib, monkeypatch):
    """对照：放得下（加上余量）照常隔离。"""
    s1, good, raw = _scene(lib)
    monkeypatch.setattr(disposal, "free_bytes", lambda path: 50 * 10**9)

    c = lib.cycle()

    assert c.applied("trash") and not raw.path.exists()


def test_restore_from_trash_is_skipped_when_the_library_has_no_room(lib, monkeypatch):
    s1, good, raw = _scene(lib)
    c = lib.cycle()
    [moved] = lib.trash_files()
    monkeypatch.setattr(disposal, "free_bytes", lambda path: 1000)

    res = lib.rollback(c.run_id)

    assert moved.exists() and not raw.path.exists()
    assert any("放不下" in d.get("skip_reason", "") for d in res["skipped_detail"])
