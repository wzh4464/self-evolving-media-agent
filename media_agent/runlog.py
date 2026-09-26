"""`run` 的日志：每一行带 ISO 时间与批次 ID，`state/run.log` / `run.err.log` 在进程里轮转。

**为什么**（runloop §4）：launchd 把 `run` 的 stdout / stderr 追加到 `state/run.log` / `run.err.log`（plist 的
`StandardOutPath` / `StandardErrorPath`）。生产上 run.log 1.8 MB / 159 轮：没有时间戳、没有批次号、没有轮次分隔、
从不轮转——出了事对不上是哪一轮打的，文件还一直涨。

## 每行带时间与批次 ID（`stamped`）

`run` 期间把 `sys.stdout` / `sys.stderr` 换成 `Stamped`：攒到换行为止，每一整行前面加 `2026-09-26T12:00:00 [批次 ID] `。
`print` 常常分两次写（正文、换行），多行字符串一次写好几行，所以按行攒、不按 `write` 调用加前缀。结束时（包括异常）
把没换行的尾巴也带上前缀写出去，再换回原来的流。只包 `run`：人手动跑的 `diagnose` / `health` 不需要这些前缀。
子进程（ffprobe、docker）的输出都被捕获了，不直接写 1 / 2 号描述符。

## 轮转（`rotate`）：先拷贝、再截断，不改名

launchd 在每轮启动时按路径打开 run.log，把描述符交给进程。进程里把 run.log **改名**成 run.log.1，本进程的 1 号
描述符仍指着那个 inode：这一轮的输出全写进了 run.log.1，新的 run.log 要到下一轮 launchd 再开时才出现——日志与
文件名错开一轮，而且改名那一刻起的输出去了"旧"文件（测试里有对照）。所以：

1. `run.log.4 → .5`、…、`.1 → .2`（最老的 `.5` 被覆盖掉：留 5 代）；
2. `run.log` 整份**拷贝**成 `run.log.1`（先写临时文件再改名）；拷不出去（磁盘满）就不截断——一行都不丢，只在 stderr
   说一句，下一轮再试；
3. `run.log` **截断**成 0，描述符继续指着它。
4. 截断之后写到哪，取决于描述符的打开方式：带 `O_APPEND` 的每次写都落在当前文件尾——开源 launchd（公开的最后一版，
   apple-oss-distributions/launchd `src/core.c` 第 5189–5190 行，2026-09-26 核对）正是以 `O_WRONLY|O_CREAT|O_APPEND`
   打开 `StandardOutPath` / `StandardErrorPath`；不带的，偏移还停在旧的文件尾，下一次写会在前面留下几 MB 的空洞（稀疏
   文件，读出来是 NUL）。如今的 launchd 不开源，所以不押在它上面：截断之后把本进程指着这个文件的描述符（按
   `st_dev` / `st_ino` 认）`lseek` 到尾。两种打开方式都有测试（去掉 `lseek`，不带 `O_APPEND` 的那个红）。

轮转在 `run` 拿到运行锁之后、打印任何东西之前做：锁保证没有另一个 media-agent 同时往这两个文件写。阈值 5 MB——
按生产的速度（1.8 MB / 159 轮）大约一年半一次；加了时间戳前缀之后会快一些。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOG_NAMES = ("run.log", "run.err.log")
# `media-agent grab`（launchd 每 30 分钟）自己的两份日志（deploy/com.zihan.media-agent-grab.plist），同样在拿到锁之后轮转
GRAB_LOG_NAMES = ("grab.log", "grab.err.log")
MAX_BYTES = 5 * 1024 * 1024
KEEP = 5


class Stamped(io.TextIOBase):
    """包一层文本流：每一整行前面加 `<ISO 时间> [<批次 ID>] `。写失败照常往外抛（与不包时一样）。"""

    def __init__(self, inner, run_id: str):
        self._inner = inner
        self._prefix_id = run_id
        self._buf = ""

    def _stamp(self) -> str:
        return f"{datetime.now().isoformat(timespec='seconds')} [{self._prefix_id}] "

    def write(self, s: str) -> int:
        self._buf += str(s)
        if "\n" in self._buf:
            *lines, self._buf = self._buf.split("\n")
            self._inner.write("".join(f"{self._stamp()}{ln}\n" for ln in lines))
        return len(s)

    def finish(self) -> None:
        """把没换行的尾巴也带上前缀写出去。"""
        if self._buf:
            tail, self._buf = self._buf, ""
            self._inner.write(f"{self._stamp()}{tail}\n")
        self.flush()

    def flush(self) -> None:
        self._inner.flush()

    def isatty(self) -> bool:
        return self._inner.isatty()

    def fileno(self) -> int:
        return self._inner.fileno()

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._inner, "errors", "strict")

    def writable(self) -> bool:
        return True


class Redacting(io.TextIOBase):
    """包一层文本流：按整行过 `scrub`（遮掉密钥）再写出去。

    2026-09-27 v0.4.0 上线第一轮：原来被静默吞掉的 TMDB 404 被说了出来，报错里带着请求 URL
    `…?api_key=<明文>…`——健康报告与邮件过了 `notify.redact`，标准输出没过，密钥原样进了
    launchd 的 run.log。打码放在输出流这一层，所有子命令、所有打印路径一次性生效。

    按行攒、不按 `write` 调用处理：`print` 常分两次写（正文、换行），一个 URL 也可能被拆开写。
    **`flush()` 也不吐出半行**：凭据的前缀与值若被拆成两次写、中间 flush 一下，正则就只看得到半截，
    而写出去的就再也遮不回来（PR #1 审查意见）。CLI 里没有交互提示，半行攒到换行或 `finish()` 再写。

    已配置的密钥按字面替换，但 `notify.redact` 对短于 `_LITERAL_MIN` 的不做字面替换（会误伤正常文本）；
    这种密钥由 `cli.warn_short_secrets` 在启动时点名告警，而不是悄悄漏过。
    """

    def __init__(self, inner, scrub):
        self._inner = inner
        self._scrub = scrub
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += str(s)
        if "\n" in self._buf:
            head, self._buf = self._buf.rsplit("\n", 1)
            self._inner.write(self._scrub(head + "\n"))
        return len(s)

    def finish(self) -> None:
        if self._buf:
            tail, self._buf = self._buf, ""
            self._inner.write(self._scrub(tail) + "\n")
        self._inner.flush()

    def flush(self) -> None:
        self._inner.flush()                  # 半行不吐：见类文档

    def isatty(self) -> bool:
        return self._inner.isatty()

    def fileno(self) -> int:
        return self._inner.fileno()

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._inner, "errors", "strict")

    def writable(self) -> bool:
        return True


@contextmanager
def redacting(scrub):
    """期间 `sys.stdout` / `sys.stderr` 的输出一律按行过 `scrub`；结束（含异常）时换回原来的流。"""
    out, err = sys.stdout, sys.stderr
    r_out, r_err = Redacting(out, scrub), Redacting(err, scrub)
    sys.stdout, sys.stderr = r_out, r_err
    try:
        yield
    finally:
        try:
            r_out.finish()
            r_err.finish()
        finally:
            sys.stdout, sys.stderr = out, err


@contextmanager
def stamped(run_id: str):
    """期间 `sys.stdout` / `sys.stderr` 的每一行都带时间与批次 ID；结束（含异常）时换回原来的流。"""
    out, err = sys.stdout, sys.stderr
    s_out, s_err = Stamped(out, run_id), Stamped(err, run_id)
    sys.stdout, sys.stderr = s_out, s_err
    try:
        yield
    finally:
        try:
            s_out.finish()
            s_err.finish()
        finally:
            sys.stdout, sys.stderr = out, err


def _same_file(fd: int, st: os.stat_result) -> bool:
    try:
        fst = os.fstat(fd)
    except OSError:
        return False
    return (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino)


def rotate(state_dir, *, names=LOG_NAMES, max_bytes: int = MAX_BYTES, keep: int = KEEP,
           fds=(1, 2)) -> list[str]:
    """超过 `max_bytes` 的日志：拷成 `.1`（旧的往后挪，留 `keep` 代）、原文件截断成 0、`fds` 里指着它的描述符
    移到文件尾。返回给人看的说明（轮转了哪个、哪个没能轮转）。**永不抛异常。**"""
    msgs: list[str] = []
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:                           # noqa: BLE001 —— 冲不出去就算了，轮转照做
            pass
    for name in names:
        p = Path(state_dir) / name
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        except OSError as e:
            msgs.append(f"⚠️  {name} 没能轮转（读不了：{type(e).__name__}: {e}）")
            continue
        if st.st_size <= max_bytes:
            continue
        # 先拷、拷成了才把旧的往后挪。以前先挪再拷：拷不出去（磁盘满——正是"一行都不丢"要应付的情形）时挪已经
        # 发生了，每一轮丢一代，一天之后只剩最老的那份；拷到一半的临时文件也留在满了的盘上（2026-09-26 复审）。
        tmp = p.with_name(f".{name}.1.tmp")
        try:
            shutil.copyfile(p, tmp)
        except Exception as e:                      # noqa: BLE001 —— 拷不出去就什么都不动：一行都不丢，下一轮再试
            try:
                tmp.unlink(missing_ok=True)
            except OSError as e2:
                msgs.append(f"⚠️  删不掉拷到一半的 {tmp.name}（{type(e2).__name__}: {e2}）")
            msgs.append(f"⚠️  {name} 没能轮转（{type(e).__name__}: {e}），原样留着，下一轮再试")
            continue
        try:
            for i in range(keep - 1, 0, -1):
                older = p.with_name(f"{name}.{i}")
                if older.exists():
                    os.replace(older, p.with_name(f"{name}.{i + 1}"))
            os.replace(tmp, p.with_name(f"{name}.1"))
        except Exception as e:                      # noqa: BLE001 —— 改名出错（同目录改名，罕见）：不截断，下一轮再试
            msgs.append(f"⚠️  {name} 没能轮转（{type(e).__name__}: {e}），原样留着，下一轮再试")
            continue
        try:
            os.truncate(p, 0)
        except OSError as e:
            msgs.append(f"⚠️  {name} 已拷成 {name}.1，但截断失败（{type(e).__name__}: {e}）")
            continue
        for fd in fds:
            if _same_file(fd, st):
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                except OSError as e:
                    msgs.append(f"⚠️  {name} 截断后移动描述符 {fd} 失败（{type(e).__name__}: {e}）")
        msgs.append(f"{name} 超过 {max_bytes // (1024 * 1024)} MB（{st.st_size / 1e6:.1f} MB），"
                    f"已轮转成 {name}.1（留 {keep} 代）")
    return msgs
