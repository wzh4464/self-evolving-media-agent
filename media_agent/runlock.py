"""跨进程运行锁：同一时刻只允许一个会改动媒体库 / 状态的进程在跑。

**为什么需要**（2026-09-26 前一把锁都没有）：

- launchd 每 6 小时起一轮 `run`；运维手动的 `apply` / `rollback` / `repair` /
  `purge --apply` 随时可能撞上它。两边各自拿一份诊断快照、各自执行，
  一方刚改的名、刚搬进隔离区的文件在另一方眼里还是旧样子（critic N17）。
- 将来每 30 分钟一次的抓取模式与 `run` 撞车时，`write_sidecar` 会用诊断时的
  快照盖掉刚抓的集，于是同一集再抓一遍（runloop §8c）。
- 部署（`deploy/deploy.sh`）切换代码时要等正在跑的一轮结束，也要挡住切换中途
  起来的新一轮——它用 `/usr/bin/lockf -k` 拿**同一个文件**上的同一种锁。

实现是 `flock(2)`：进程退出（包括被 kill）时内核自动释放，不存在"残留锁文件"
要人去删；macOS 的 `lockf(1)`（`O_EXLOCK`）与 Linux 的 `flock(1)` 用的都是它，
所以 shell 脚本和本模块互相排斥。锁文件本身**永不删除**——删了它，持有者锁住的
是一个已经没有名字的 inode，下一个按路径打开的人会拿到一把新锁，两边同时进场。
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from datetime import datetime
from pathlib import Path

LOCK_NAME = "run.lock"

# 拿不到锁时最多等多久（秒）。一轮 `run` 约 1.5–2 分钟（runloop §6），等满它不划算：
# launchd 的下一轮 6 小时后自然会来，手动命令让人重试即可。短暂等待只为吸收
# "上一个进程正在退出"这种边界。
DEFAULT_WAIT = 10.0
_POLL = 0.2


class RunLock:
    """`state/run.lock` 上的排他锁。

        lock = RunLock(cfg.state_dir / LOCK_NAME, "media-agent run")
        if not lock.acquire():
            print("被占用：", lock.holder())
        try: ...
        finally: lock.release()

    拿到锁后把 `pid / 命令 / 开始时间` 写进锁文件，拿不到的一方据此说清楚
    "谁占着"。内容只是给人看的说明，判定只认 flock。
    """

    def __init__(self, path: Path | str, label: str = ""):
        self.path = Path(path)
        self.label = label
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, wait: float = DEFAULT_WAIT) -> bool:
        """非阻塞地试，拿不到就每 0.2 秒再试，最多等 `wait` 秒。"""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(wait, 0.0)
        swapped = 0
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os.close(fd)
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                time.sleep(min(_POLL, left))
                continue
            # 锁住的可能是一个刚被删掉的旧 inode（有人手动 rm 了锁文件，或不带 -k 的
            # lockf 退出时删的）：那把锁不排斥任何按路径新开文件的人。对不上就放掉重来。
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if not same:
                os.close(fd)
                swapped += 1
                if swapped > 50:                      # 防御：路径被人反复替换，别死循环
                    return False
                continue
            self._fd = fd
            self._describe()
            return True

    def _describe(self) -> None:
        info = (f"pid={os.getpid()} cmd={self.label or '?'} "
                f"since={datetime.now().isoformat(timespec='seconds')}\n")
        try:
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, info.encode("utf-8"), 0)
        except OSError:
            pass                                      # 说明写不进去不影响锁本身

    def holder(self) -> str:
        """当前持有者自述（拿不到锁时用来报告）；读不到就返回空串。"""
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.ftruncate(fd, 0)                       # 清掉自述，免得下一个人被旧信息误导
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "RunLock":
        if not self.acquire():
            raise TimeoutError(f"运行锁被占用：{self.holder() or '持有者未知'}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
