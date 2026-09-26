"""回归测试：命令行输出（进 launchd 的 run.log）里不许出现密钥。

2026-09-27 v0.4.0 上线后的第一轮：第 3 阶段把原来被静默吞掉的异常逐个说出来，
其中 TMDB 的 404 带着请求 URL——`...?api_key=<明文>&language=zh-CN`。健康报告与通知邮件
都过了 `notify.redact`，标准输出却没有，于是密钥原样进了 run.log，且每跑一轮再写一次。

所以打码要放在**输出流**这一层，对所有子命令、所有打印路径（print、`_log`、`ctx.log`、
`run` 的时间戳层）一次性生效，而不是指望每个打印点记得自己打码。
"""
from __future__ import annotations

import io
import sys

from media_agent import cli, runlog

KEY = "0123456789abcdef0123456789abcdef"
URL = f"https://api.themoviedb.org/3/tv/240411/season/2?api_key={KEY}&language=zh-CN"


def _scrub(s: str) -> str:
    return s.replace(KEY, "***")


def test_redacting_stream_scrubs_whole_lines():
    buf = io.StringIO()
    w = runlog.Redacting(buf, _scrub)
    w.write("TMDB 失败：")          # print 常分两次写：正文、换行
    w.write(URL)
    w.write("\n")
    w.write(f"second {KEY}\nthird")
    w.finish()
    out = buf.getvalue()
    assert KEY not in out
    assert out == "TMDB 失败：" + URL.replace(KEY, "***") + "\nsecond ***\nthird\n"


def test_flush_emits_the_partial_line_scrubbed():
    buf = io.StringIO()
    w = runlog.Redacting(buf, _scrub)
    w.write(f"prompt {KEY}")
    w.flush()
    assert buf.getvalue() == "prompt ***"
    w.write(" tail\n")
    assert buf.getvalue() == "prompt *** tail\n"


def test_redacting_context_wraps_stdout_and_stderr_and_restores(capsys):
    out0, err0 = sys.stdout, sys.stderr
    with runlog.redacting(_scrub):
        print(URL)
        print(f"err {KEY}", file=sys.stderr)
    assert (sys.stdout, sys.stderr) == (out0, err0)
    got = capsys.readouterr()
    assert KEY not in got.out and KEY not in got.err
    assert "api_key=***" in got.out


def test_stamped_run_output_is_scrubbed_too(capsys):
    with runlog.redacting(_scrub), runlog.stamped("RID"):
        print(URL)
    got = capsys.readouterr().out
    assert KEY not in got
    assert "[RID]" in got and "api_key=***" in got


def test_every_cli_command_output_goes_through_redaction(monkeypatch, capsys):
    """main() 对任何子命令都先装上打码层：`cli._log`（也就是 ctx.log 的出口）打出来的也遮掉。"""
    class Cfg:
        tmdb_api_key = KEY
        qbit_pass = ab_pass = llm_key = notify_smtp_pass = ""

    monkeypatch.setattr(cli, "load_config", lambda: Cfg())

    def fake_cmd(args, cfg):
        cli._log(f"[incomplete-season] TMDB 第 2 季集表读取失败：404 for url '{URL}'")
        print(f"stdout 里也有 {KEY}")
        return 0

    monkeypatch.setattr(cli, "cmd_runs", fake_cmd)
    monkeypatch.setattr(sys, "argv", ["media-agent", "runs"])
    assert cli.main() == 0
    got = capsys.readouterr()
    assert KEY not in got.out and KEY not in got.err
    assert "api_key=***" in got.err
