"""测试基座自己的测试：假对象的语义要和生产上实测的 qBittorrent v5.2.3 一致，
tripwire 要真的能拦住"空洞的绿灯"。

基座错了，建在它上面的每个测试都在测一个不存在的世界——所以这些断言
写的都是**生产上实测过的事实**（testinfra 调查，2026-09-26 只读核对 539 个种子）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import textwrap
import urllib.request
from pathlib import Path

import httpx
import pytest

from harness import make_torrent, read_ident, video
from media_agent import config as config_mod
from media_agent import probe as probe_mod
from media_agent.actions import Executor
from media_agent.clients import QBitError
from media_agent.kernel import Action, Finding


# ============================================================ FakeQbit 语义
def test_content_path_follows_v523_layout_rules(lib):
    s1 = lib.show("测试番").season(1)
    single = s1.single("测试番 S01E01.mkv", size=100_000_000)
    orig = s1.torrent({"a.mkv": 1_000_000, "b.mkv": 2_000_000}, name="[G] Show 01-02")
    nosub = s1.torrent({"c.mkv": 3_000_000, "d.mkv": 4_000_000}, name="[G] Show 03-04",
                       layout="nosub")

    assert single.view()["content_path"] == str(s1.path / "测试番 S01E01.mkv")
    assert orig.view()["content_path"] == str(s1.path / "[G] Show 01-02")
    assert orig.view()["root_path"] == str(s1.path / "[G] Show 01-02")
    # NoSubfolder 多文件：content_path 就是 Season 目录本身（B1 的前提）
    assert nosub.view()["content_path"] == str(s1.path)
    assert nosub.view()["root_path"] == ""
    assert (s1.path / "[G] Show 01-02" / "a.mkv").stat().st_size == 1_000_000
    assert (s1.path / "c.mkv").stat().st_size == 3_000_000


def test_rename_file_keeps_torrent_name_and_moves_partial(lib):
    s1 = lib.show("测试番").season(1)
    t = s1.single("[G] Show - 01 [1080p].mkv", size=50_000_000, progress=0.5)
    assert (s1.path / "[G] Show - 01 [1080p].mkv.!qB").exists()

    lib.qbit.rename_file(t.hash, "[G] Show - 01 [1080p].mkv", "测试番 S01E01.mkv")

    v = t.view()
    assert v["name"] == "[G] Show - 01 [1080p].mkv"          # 显示名不变（AGENTS.md 第 2 条）
    assert v["content_path"] == str(s1.path / "测试番 S01E01.mkv")
    assert (s1.path / "测试番 S01E01.mkv.!qB").exists()       # 半成品跟着改名
    assert not (s1.path / "[G] Show - 01 [1080p].mkv.!qB").exists()
    assert lib.qbit.file_names(t.hash) == ["测试番 S01E01.mkv"]


@pytest.mark.allow("qbit_error")
def test_rename_file_conflicts_are_409(lib):
    s1 = lib.show("测试番").season(1)
    t = s1.torrent({"a.mkv": 1_000_000, "b.mkv": 1_000_000}, name="R", layout="nosub")
    with pytest.raises(QBitError, match="HTTP 409"):
        lib.qbit.rename_file(t.hash, "nope.mkv", "x.mkv")
    with pytest.raises(QBitError, match="HTTP 409"):
        lib.qbit.rename_file(t.hash, "a.mkv", "b.mkv")         # 目标被同种子另一条目占用


def test_rename_file_without_file_on_disk_only_remaps(lib):
    s1 = lib.show("测试番").season(1)
    t = s1.single("old.mkv", size=10_000_000, on_disk=False)
    lib.qbit.rename_file(t.hash, "old.mkv", "new.mkv")
    assert lib.qbit.file_names(t.hash) == ["new.mkv"]
    assert not (s1.path / "new.mkv").exists()


def test_rename_overwrite_is_reported(lib, tripwire):
    s1 = lib.show("测试番").season(1)
    t = s1.single("a.mkv", size=1_000_000)
    s1.local("b.mkv", size=2_000_000)
    lib.qbit.rename_file(t.hash, "a.mkv", "b.mkv")
    assert [e.kind for e in tripwire.events] == ["qbit_overwrite"]
    assert (s1.path / "b.mkv").stat().st_size == 1_000_000
    tripwire.allow("qbit_overwrite")


def test_tags_are_sorted_and_comma_space_joined(lib):
    t = lib.show("测试番").season(1).single("x.mkv", tags="ma:S01E12,ab:33")
    assert t.view()["tags"] == "ab:33, ma:S01E12"
    lib.qbit.add_tags([t.hash], "zz, aa")
    lib.qbit.remove_tags([t.hash], "ma:S01E12")
    assert t.view()["tags"] == "aa, ab:33, zz"


def test_files_on_unknown_hash_raises_same_error_as_real_client(lib, tripwire):
    with pytest.raises(QBitError) as ei:
        lib.qbit.files("f" * 40)
    assert str(ei.value) == "torrents/files -> HTTP 404: Not Found"
    assert [e.kind for e in tripwire.events] == ["qbit_error"]
    tripwire.allow("qbit_error")


def test_priority_zero_keeps_file_on_disk_and_shrinks_size(lib):
    s1 = lib.show("测试番").season(1)
    t = s1.torrent({"tv.mp4": 700, "uncut.mp4": 800}, name="B", layout="nosub")
    lib.qbit.set_file_priority(t.hash, [0], 0)
    assert (s1.path / "tv.mp4").exists()                     # use_unwanted_folder=False
    assert t.view()["size"] == 800
    assert [f["priority"] for f in lib.qbit.files(t.hash)] == [0, 1]


def test_delete_without_files_only_drops_record(lib):
    s1 = lib.show("测试番").season(1)
    keep = s1.single("keep.mkv", size=1_000)
    gone = s1.single("gone.mkv", size=1_000)
    lib.qbit.delete([keep.hash], delete_files=False)
    lib.qbit.delete([gone.hash], delete_files=True)
    lib.qbit.delete(["0" * 40], delete_files=False)          # 未知 hash：真 API 也静默
    assert (s1.path / "keep.mkv").exists() and not lib.qbit.has(keep.hash)
    assert not (s1.path / "gone.mkv").exists()


def test_add_torrent_infohash_matches_executor_and_409_is_false(lib):
    blob, h = make_torrent("[G] Show - 09.mkv", size=5_000_000)
    assert Executor._infohash_v1(blob) == h                    # 与执行器同一口径
    sp = lib.show("测试番").season(1).path
    assert lib.qbit.add_torrent(blob, save_path=str(sp), category="测试番",
                                tags="ma:S01E09") is True
    assert lib.qbit.add_torrent(blob, save_path=str(sp)) is False
    v = lib.qbit.torrent(h)
    assert (v["progress"], v["state"], v["tags"]) == (0, "downloading", "ma:S01E09")


def test_add_multi_file_torrent_nosubfolder_strips_root(lib):
    blob, h = make_torrent("[G] Batch", files={"e01.mkv": 10, "e02.mkv": 20})
    sp = lib.show("测试番").season(1).path
    lib.qbit.add_torrent(blob, save_path=str(sp))               # no_subfolder=True（本项目默认）
    assert lib.qbit.file_names(h) == ["e01.mkv", "e02.mkv"]
    assert lib.qbit.torrent(h)["content_path"] == str(sp)


def test_fault_injection_times_out_once_for_one_hash(lib):
    s1 = lib.show("测试番").season(1)
    a, b = s1.single("a.mkv"), s1.single("b.mkv")
    lib.qbit.fail("files", hash=a.hash)
    with pytest.raises(httpx.ReadTimeout):
        lib.qbit.files(a.hash)
    assert lib.qbit.files(b.hash)                              # 别的种子不受影响
    assert lib.qbit.files(a.hash)                              # 只注入一次


def test_unmodeled_method_trips(lib, tripwire):
    with pytest.raises(AttributeError):
        lib.qbit.trackers("a" * 40)
    assert [e.kind for e in tripwire.events] == ["unmodeled"]
    tripwire.allow("unmodeled")


# ============================================================ 探测替身
def test_fake_probe_follows_file_across_rename(lib):
    s1 = lib.show("测试番").season(1)
    t = s1.single("[G] Show - 01.mkv", size=30_000_000,
                  probe=video("hevc", subs=["chi JPSC", "chi JPTC"]))
    lib.qbit.rename_file(t.hash, "[G] Show - 01.mkv", "测试番 S01E01.mkv")
    info = probe_mod.probe(s1.path / "测试番 S01E01.mkv")      # 走真实的 probe() 解析
    assert info.vcodec == "hevc" and info.sub_marks == ("chi JPSC", "chi JPTC")
    assert info.has_simplified
    assert probe_mod.probe(s1.path / "nope.mkv") is None


def test_unregistered_file_probes_as_unavailable(make_file):
    f = make_file("x.mkv")
    assert probe_mod.probe(f.path) is None


@pytest.mark.ffmpeg
def test_fake_probe_json_parses_like_real_ffprobe(tmp_path, fake_probe):
    """FakeProbe 合成的 JSON 必须和真 ffprobe 的输出被 probe() 解析成同一个结果。"""
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,900\n字幕\n", encoding="utf-8")
    mkv = tmp_path / "tiny.mkv"
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-i", str(srt), "-i", str(srt),
         "-map", "0:v", "-map", "1:a", "-map", "2", "-map", "3", "-shortest",
         "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
         "-metadata:s:s:0", "language=chi", "-metadata:s:s:0", "title=JPSC",
         "-metadata:s:s:1", "language=chi", "-metadata:s:s:1", "title=JPTC",
         str(mkv)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    real = probe_mod.probe(mkv)
    assert real is not None and real.vcodec == "h264"

    ident = "parity"
    from harness import write_sparse
    fake_path = tmp_path / "fake.mkv"
    write_sparse(fake_path, 1000, ident)
    fake_probe.register(ident, video("h264", height=real.height, duration=real.duration,
                                     subs=list(real.sub_marks)))
    orig = probe_mod._run
    try:
        probe_mod._run = fake_probe.run
        fake = probe_mod.probe(fake_path)
    finally:
        probe_mod._run = orig
    assert (fake.vcodec, fake.height, fake.sub_count, fake.sub_marks) == \
           (real.vcodec, real.height, real.sub_count, real.sub_marks)
    assert abs(fake.duration - real.duration) < 1e-3


# ============================================================ 隔离
def test_state_lives_in_tmp_and_dotenv_is_not_read(lib, project_root):
    assert lib.cfg.state_dir == project_root / "state"
    assert lib.cfg.trash_dir.is_relative_to(project_root)
    cfg = config_mod.load_config()                              # 仓库里的真 .env 不得生效
    assert (cfg.qbit_pass, cfg.ab_pass, cfg.tmdb_api_key, cfg.llm_key) == ("", "", "", "")
    assert "QBIT_PASS" not in os.environ


def test_builder_files_are_sparse_and_carry_identity(lib):
    p = lib.show("测试番").season(1).local("x.mkv", size=2_000_000_000)
    assert p.stat().st_size == 2_000_000_000
    assert read_ident(p).startswith("local:")


def test_qbit_down_makes_every_file_local(lib):
    lib.show("测试番").season(1).single("测试番 S01E01.mkv")
    lib.qbit_down()
    st = lib.scan()
    assert [f.torrent_hash for s in st.shows for f in s.files] == [""]


# ============================================================ tripwire 接线
def test_unrouted_url_is_recorded(tripwire):
    with pytest.raises(OSError):                               # URLError 是 OSError
        urllib.request.urlopen("https://mikanani.me/RSS/Bangumi?bangumiId=1")
    assert [e.kind for e in tripwire.events] == ["unrouted_url"]
    tripwire.clear()


def test_socket_and_subprocess_are_blocked(tripwire):
    with pytest.raises(OSError):
        socket.create_connection(("qbit.invalid", 80), timeout=0.1)
    with pytest.raises(FileNotFoundError):
        subprocess.run(["/usr/local/bin/docker", "stop", "autobangumi"])
    assert [e.kind for e in tripwire.events] == ["network", "subprocess"]
    tripwire.clear()


def test_failed_audit_and_swallowed_detector_error_are_recorded(lib, tripwire):
    class Boom:
        id = "boom"

        def detect(self, ctx, state):
            raise RuntimeError("假对象少实现了一个方法")
            yield  # pragma: no cover

    assert lib.diagnose(detectors=[Boom()]) == []              # Registry 吞掉了异常……
    f = Finding(rule="t", kind="unrenamed", severity="minor", summary="x",
                action=Action(op="rename", args={"path": str(lib.media_root / "x"),
                                                 "new_name": "y", "torrent_hash": "a" * 40}))
    # 用超时而不是 404：种子已不在（404）如今是"状态变了"，记 skipped（testinfra B2）
    lib.qbit.fail("files", exc=httpx.ReadTimeout("timed out (injected)"))
    report = lib.apply([f])
    assert len(report.failed) == 1                             # ……执行器也吞掉了
    assert sorted(e.kind for e in tripwire.events) == ["detector_error", "failed_record"]
    tripwire.clear()                                           # ……但 tripwire 都看见了


def test_undeclared_tripwire_event_fails_the_test(pytester):
    """元测试：真的起一个 pytest，确认未声明的事件会让测试变红、声明了就放行。"""
    here = Path(__file__).resolve().parent
    pytester.makeconftest((here / "conftest.py").read_text(encoding="utf-8"))
    pytester.makeini(textwrap.dedent(f"""
        [pytest]
        pythonpath = {here}
        markers =
            live: x
            ffmpeg: x
            allow: x
    """))
    pytester.makepyfile(test_inner=textwrap.dedent("""
        import urllib.request
        import pytest
        from media_agent.kernel import Action, Finding

        def _hit():
            try:
                urllib.request.urlopen("https://example.invalid/feed")
            except OSError:
                pass            # 被测代码常见的"网络失败就跳过"

        def test_silently_skips():
            _hit()              # 断言全过，但它什么都没测

        @pytest.mark.allow("unrouted_url")
        def test_declared():
            _hit()

        def test_failed_record(lib):
            f = Finding(rule="t", kind="k", severity="minor", summary="x",
                        action=Action(op="grab_episode", args={
                            "url": "https://example.invalid/x.torrent",
                            "show_dir": str(lib.media_root / "s"), "season": 1, "episode": 1}))
            lib.apply([f])
    """))
    res = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    res.assert_outcomes(passed=3, errors=2)
    res.stdout.fnmatch_lines(["*unrouted_url*", "*failed_record*"])
