"""`run` 的输出：每一行带 ISO 时间与批次 ID；`state/run.log` / `run.err.log` 超过 5 MB 就在进程里轮转（留 5 代）。

为什么（runloop §4）：launchd 把 stdout / stderr 追加到这两个文件，生产上 run.log 1.8 MB / 159 轮，没有时间戳、
没有批次号、没有轮次分隔、从不轮转——出了事对不上是哪一轮打的。

**轮转不能靠改名**：launchd 在每轮启动时按 `StandardOutPath` 打开文件，把描述符交给进程。进程里把 run.log
改名成 run.log.1，本进程的 1 号描述符仍指着那个 inode——这一轮的输出全进了 run.log.1，新的 run.log 要到下一轮
launchd 再开时才出现。所以是**先拷贝、再截断**：描述符一直指着 run.log。截断之后写的位置取决于打开方式：
带 `O_APPEND`（launchd 就是这么开的）每次写都落在文件尾，没带的话偏移还停在旧的文件尾，下一次写会在前面
留下一个几 MB 的空洞——所以截断之后把本进程指着这个文件的描述符 `lseek` 到尾。两种打开方式都测。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from media_agent import cli, runlog

MB = 1024 * 1024
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d \[(?P<rid>[^\]]+)\] (?P<text>.*)$")


# ------------------------------------------------------------------ 每行带时间与批次 ID
def test_every_line_is_stamped_including_partial_writes_and_multiline_strings(capsys):
    with runlog.stamped("20260926T120000.000-1"):
        print("一行")
        sys.stdout.write("半")
        sys.stdout.write("行\n")
        print("多\n行", file=sys.stderr)
        print()
        sys.stdout.write("没有换行就结束")

    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert [STAMP.match(ln).group("text") for ln in lines] == ["一行", "半行", "", "没有换行就结束"]
    assert {STAMP.match(ln).group("rid") for ln in lines} == {"20260926T120000.000-1"}
    assert [STAMP.match(ln).group("text") for ln in err.splitlines()] == ["多", "行"]


def test_streams_are_restored_even_when_the_body_raises():
    before = sys.stdout, sys.stderr
    with pytest.raises(RuntimeError):
        with runlog.stamped("r"):
            raise RuntimeError("x")
    assert (sys.stdout, sys.stderr) == before


def test_main_run_stamps_its_output_with_the_run_id(monkeypatch, capsys):
    seen = {}

    def fake_run(args, cfg):
        seen["run_id"] = args.run_id
        print("诊断：0 个问题")
        print("⚠️  一条警告", file=sys.stderr)
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["media-agent", "run"])

    assert cli.main() == 0

    out, err = capsys.readouterr()
    rid = seen["run_id"]
    assert all(STAMP.match(ln) and STAMP.match(ln).group("rid") == rid for ln in out.splitlines())
    assert any(ln.endswith("诊断：0 个问题") for ln in out.splitlines())
    assert "开始" in out.splitlines()[0]                        # 轮次分隔：一眼看出一轮从哪开始
    assert all(STAMP.match(ln).group("rid") == rid for ln in err.splitlines())


def test_main_run_uses_one_run_id_for_the_log_prefix_the_report_and_the_snapshot(lib, monkeypatch,
                                                                                  capsys):
    """日志前缀、健康报告、发现历史是同一个批次 ID——出事时拿 run.log 里的一行就能找到那一轮的报告。上面那条把
    cmd_run 换成了替身，cmd_run 自己另起一个 ID 全套照绿（复审变异 H1i）。"""
    from media_agent import health, history
    lib.tmdb.enabled = True
    monkeypatch.setattr(cli, "load_config", lambda: lib.cfg)
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    monkeypatch.setattr(sys, "argv", ["media-agent", "run"])

    assert cli.main() == 0

    out, _ = capsys.readouterr()
    [rid] = {STAMP.match(ln).group("rid") for ln in out.splitlines() if STAMP.match(ln)}
    assert health.load_report(lib.cfg.state_dir)["run_id"] == rid
    assert [s.run_id for s in history.load_snapshots(lib.cfg.state_dir)] == [rid]


def test_other_commands_are_not_stamped(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_runs", lambda args, cfg: print("批次列表") or 0)
    monkeypatch.setattr(sys, "argv", ["media-agent", "runs"])
    assert cli.main() == 0
    assert capsys.readouterr().out == "批次列表\n"


# ------------------------------------------------------------------ 轮转
def _fill(p, n, byte=b"x"):
    p.write_bytes(byte * n)


@pytest.mark.parametrize("append", [True, False], ids=["O_APPEND（launchd）", "无 O_APPEND"])
def test_rotation_copies_then_truncates_so_the_open_descriptor_keeps_writing_to_run_log(
        tmp_path, append):
    log = tmp_path / "run.log"
    _fill(log, 6 * MB)
    flags = os.O_WRONLY | (os.O_APPEND if append else 0)
    fd = os.open(log, flags)                                    # launchd 交给进程的那个描述符
    os.lseek(fd, 0, os.SEEK_END)
    try:
        msgs = runlog.rotate(tmp_path, fds=(fd,))
        os.write(fd, b"after\n")
    finally:
        os.close(fd)

    assert log.read_bytes() == b"after\n"                       # 没有空洞、没写进 .1
    assert (tmp_path / "run.log.1").stat().st_size == 6 * MB
    assert any("run.log" in m for m in msgs)


def test_rename_based_rotation_would_lose_the_live_descriptor(tmp_path):
    """对照：改名之后，这个描述符写进的是 run.log.1，run.log 根本不存在——这就是不用改名的原因。"""
    log = tmp_path / "run.log"
    _fill(log, 10)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND)
    os.rename(log, tmp_path / "run.log.1")
    os.write(fd, b"after\n")
    os.close(fd)
    assert not log.exists() and (tmp_path / "run.log.1").read_bytes().endswith(b"after\n")


def test_keeps_five_generations_and_drops_the_oldest(tmp_path):
    for i in range(1, 6):
        (tmp_path / f"run.log.{i}").write_text(f"gen{i}")
    _fill(tmp_path / "run.log", 6 * MB, b"n")

    runlog.rotate(tmp_path, fds=())

    assert (tmp_path / "run.log.1").stat().st_size == 6 * MB
    assert [(tmp_path / f"run.log.{i}").read_text() for i in range(2, 6)] == \
        ["gen1", "gen2", "gen3", "gen4"]
    assert not (tmp_path / "run.log.6").exists()
    assert (tmp_path / "run.log").stat().st_size == 0


def test_small_logs_and_missing_logs_are_left_alone(tmp_path):
    _fill(tmp_path / "run.log", 5 * MB)                         # 正好 5 MB：不轮转
    assert runlog.rotate(tmp_path, fds=()) == []
    assert (tmp_path / "run.log").stat().st_size == 5 * MB and not (tmp_path / "run.log.1").exists()


def test_both_logs_rotate_independently(tmp_path):
    _fill(tmp_path / "run.log", 1 * MB)
    _fill(tmp_path / "run.err.log", 6 * MB)
    runlog.rotate(tmp_path, fds=())
    assert (tmp_path / "run.err.log.1").exists() and not (tmp_path / "run.log.1").exists()


def test_rotation_problems_are_reported_not_raised(tmp_path, monkeypatch):
    _fill(tmp_path / "run.log", 6 * MB)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runlog.shutil, "copyfile", boom)

    msgs = runlog.rotate(tmp_path, fds=())

    assert any("没能轮转" in m and "No space left" in m for m in msgs)
    assert (tmp_path / "run.log").stat().st_size == 6 * MB      # 拷不出去就不截断：一行都不丢


def test_a_failing_copy_leaves_every_generation_alone_run_after_run(tmp_path, monkeypatch):
    """磁盘满（文档里说"一行都不丢"的那种情形）：以前先把 .4→.5 … .1→.2 挪完再拷，拷不出去时挪已经发生了——
    每一轮丢掉一代，一天（4 轮）之后只剩最老的那份；拷到一半的 .run.log.1.tmp 也留在已经满了的盘上
    （2026-09-26 复审）。现在先拷、拷成了才挪。"""
    for i in range(1, 6):
        (tmp_path / f"run.log.{i}").write_text(f"gen{i}")
    _fill(tmp_path / "run.log", 6 * MB)

    def half_then_full(src, dst, *a, **k):
        Path(dst).write_bytes(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runlog.shutil, "copyfile", half_then_full)

    for _ in range(5):
        msgs = runlog.rotate(tmp_path, fds=())
        assert any("没能轮转" in m for m in msgs)

    assert [(tmp_path / f"run.log.{i}").read_text() for i in range(1, 6)] == \
        ["gen1", "gen2", "gen3", "gen4", "gen5"]
    assert (tmp_path / "run.log").stat().st_size == 6 * MB
    assert not (tmp_path / ".run.log.1.tmp").exists()


def test_main_run_rotates_before_printing(monkeypatch, capsys, project_root):
    state = project_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    _fill(state / "run.log", 6 * MB)
    monkeypatch.setattr(cli, "cmd_run", lambda args, cfg: 0)
    monkeypatch.setattr(sys, "argv", ["media-agent", "run"])

    assert cli.main() == 0

    assert (state / "run.log.1").stat().st_size == 6 * MB
    assert (state / "run.log").stat().st_size == 0
    assert "run.log" in capsys.readouterr().err                 # 说一句轮转了
