"""路径占用：一个绝对路径此刻归谁。

**任何要在媒体库里"落一个名字"的动作，落笔之前都问这一句。** 改名、抓取后的即时改名、
relink、回退里的逆改名 / 重加种子 / 从隔离区搬回、目录改名的目的地——它们以前各查各的：
`_op_rename` 只看盘上 `target.exists()`，其余一概不查（critic N6）。

为什么盘上看不到不等于没人占——qBittorrent 5.2.3 / libtorrent 2.0.13 的实际语义：

- `torrents/renameFile` 的 409 只管**同一个种子内部**的重名，跨种子不查。
- libtorrent 只在**源文件存在**且目标也存在时才报 "File exists"。源文件还没落盘
  （0%、刚拿到元数据）时只改映射；下载中的文件在盘上叫 `X.!qB`，改名是
  `A.!qB → X.!qB`，从不和完整的 `X` 比。于是两个种子无声无息地宣称同一个路径，
  直到完成那一刻 `X.!qB → X` 撞上 EEXIST。
- 生产 2026-08-29 穹庐下的魔女 S01E09：`- 09` 下载中被改到 X（数据还在 `X.!qB`），
  `- 09v2` 完成后也被改到 X——盘上没有 X，照改不误；留下 789MB 的孤儿 `X.!qB`。
  2026-09-06 尼古喵喵 S01E08：两个种子声明同一路径，判重把唯一的真文件当输家清走。

所以"占用"有两个来源，**任何一个**命中就算：

1. **盘上**：`X` 或 `X.!qB` 存在（`incomplete_files_ext=True`，半成品带后缀）。
   问的人自己的文件（`own_path` 或 `own_path.!qB`，按 inode 认）不算。
2. **qBittorrent**：另一个种子有优先级非 0 的条目，`save_path + 条目名` 就是 `X`
   （或 `X.!qB`）——0%、刚拿到元数据、还一个字节没下的也算。问的人自己的那个条目不算。

**比较口径是 NFC + casefold**（`fold`）：生产媒体卷是大小写不敏感、规范化不敏感的
APFS，`S01E08.mkv` 与 `s01e08.MKV` 是同一个文件，`ガ` 与 `カ`+浊点也是。盘上那一侧
按目录列表折叠比较，不依赖宿主文件系统（CI 的 Linux 区分大小写）。只改大小写的
自我改名因此自然放行：撞上的正是自己。

**看不全就不知道**（fail closed）：qBittorrent 不可用、`torrents()` 读失败、相关种子的
`files()` 读失败、目录读不了——`ClaimCheck.unknown` 非空，调用方必须拒绝。
`files()` 404（列出之后被删了）不算看不全：它已经不声明任何东西。

**索引按需建、一批次一份**：`torrents()` 一次；`files()` 只问 save_path 是目标祖先的
种子（生产 539 个种子，一个 Season 目录约 40 个），问过的缓存住。执行器每做完一次改动
（任何非 skipped 的审计，以及抓取加种之后）就 `invalidate()`——看到的永远是自己上一步
之后的状态。批次之外的并发改动（AutoBangumi 每 60 秒的改名）缓存与否都挡不住，
那是运行锁与所有权分类的事。
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PARTIAL = ".!qB"


def fold(p: str | Path) -> str:
    """APFS 上"是不是同一个名字"的口径：NFC 规范化 + casefold。"""
    return unicodedata.normalize("NFC", str(p)).casefold()


@dataclass(frozen=True)
class Claimant:
    """一个占用者。`kind` 是 `"disk"`（盘上已有）或 `"qbit"`（种子声明着）。"""
    kind: str
    path: str                      # 撞上的绝对路径：盘上的实际名字，或 save_path/条目名
    partial: bool = False          # 撞的是 `.!qB` 半成品
    hash: str = ""
    name: str = ""                 # 种子显示名
    entry: str = ""                # 种子内相对路径
    progress: float | None = None
    state: str = ""
    category: str = ""

    def brief(self) -> str:
        base = os.path.basename(self.path)
        if self.kind == "disk":
            return f"盘上已有{'半成品 ' if self.partial else ''}{base}"
        pct = "" if self.progress is None else f"{self.progress * 100:.0f}%，"
        return (f"种子 {self.hash[:8]}（{self.name[:40]}，{pct}{self.state or '?'}）"
                f"声明着 {base}")

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "path": self.path}
        if self.partial:
            d["partial"] = True
        for k in ("hash", "name", "entry", "state", "category"):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.progress is not None:
            d["progress"] = round(self.progress, 3)
        return d


@dataclass
class ClaimCheck:
    """一次占用查询的结果。`free` 才能写；`unknown` 非空时调用方必须拒绝。"""
    target: str
    claimants: list[Claimant] = field(default_factory=list)
    unknown: str = ""

    @property
    def free(self) -> bool:
        return not self.unknown and not self.claimants

    @property
    def on_disk(self) -> list[Claimant]:
        return [c for c in self.claimants if c.kind == "disk"]

    @property
    def in_qbit(self) -> list[Claimant]:
        return [c for c in self.claimants if c.kind == "qbit"]

    def describe(self, limit: int = 2) -> str:
        if self.unknown:
            return f"无法确认占用情况：{self.unknown}"
        parts = [c.brief() for c in self.claimants[:limit]]
        more = len(self.claimants) - limit
        return "；".join(parts) + (f"；另有 {more} 处" if more > 0 else "")

    def audit(self) -> dict:
        """写进审计记录的形状。"""
        d: dict = {"target": self.target,
                   "claimants": [c.as_dict() for c in self.claimants]}
        if self.unknown:
            d["unknown"] = self.unknown
        return d


class ClaimsUnknown(Exception):
    """看不全：qBittorrent 或磁盘读不到。`ClaimIndex.check*` 把它折成 `unknown`。"""


def _is_404(e: BaseException) -> bool:
    return "404" in str(e)


def _lstat_key(p: Path) -> tuple[int, int] | None:
    try:
        st = os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise ClaimsUnknown(f"读不了 {p}：{type(e).__name__}: {e}") from e
    return st.st_dev, st.st_ino


def _disk_hits(target: Path, *, partial: bool = True) -> dict[tuple[int, int], tuple[Path, bool]]:
    """盘上与 `target`（及 `target.!qB`）同名——按 `fold` 口径——的条目。

    先直接 lstat（APFS 上它本来就大小写 / 规范化不敏感，父目录名大小写不同也认得），
    再列父目录按折叠后的名字比（区分大小写的文件系统上靠这一步）。同一个 inode 只算一次。
    返回 `{(st_dev, st_ino): (路径, 是否半成品)}`。
    """
    want = {fold(target.name): False}
    if partial:
        want[fold(target.name + PARTIAL)] = True
    hits: dict[tuple[int, int], tuple[Path, bool]] = {}

    def add(p: Path, is_partial: bool) -> None:
        key = _lstat_key(p)
        if key is not None:
            hits.setdefault(key, (p, is_partial))

    add(target, False)
    if partial:
        add(Path(str(target) + PARTIAL), True)
    try:
        with os.scandir(target.parent) as it:
            for de in it:
                is_partial = want.get(fold(de.name))
                if is_partial is not None:
                    add(target.parent / de.name, is_partial)
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as e:
        raise ClaimsUnknown(f"读不了目录 {target.parent}：{type(e).__name__}: {e}") from e
    return hits


class ClaimIndex:
    """一批次共用的占用索引。见模块文档。

    - `qbit`：`QBitClient`（或 FakeQbit）；None = qBittorrent 不可用，一切查询都是 unknown。
    - `ignore`：本批次已经摘掉的种子 hash。**按引用保存**——执行器往自己的
      `_removed_torrents` 里加，这里立刻看得到（qBittorrent 的删除是异步的，
      刚删完的种子可能还会在列表里出现一会儿）。
    """

    def __init__(self, qbit, *, ignore: set[str] | frozenset = frozenset()):
        self.qbit = qbit
        self.ignore = ignore
        self._list: dict[str, dict] | None = None
        self._files: dict[str, list[dict]] = {}

    def invalidate(self) -> None:
        """作废全部缓存。执行器每做完一次改动就调——下一次查询重新问 qBittorrent。"""
        self._list = None
        self._files = {}

    # ---------------------------------------------------------------- 读（带缓存）
    def torrents(self) -> dict[str, dict]:
        """`{hash: torrents() 视图}`。读不到抛 `ClaimsUnknown`。"""
        if self.qbit is None:
            raise ClaimsUnknown("qBittorrent 不可用（登录失败或未配置）")
        if self._list is None:
            try:
                ts = self.qbit.torrents()
            except Exception as e:
                raise ClaimsUnknown(f"torrents() 读取失败：{type(e).__name__}: {e}") from e
            self._list = {(t.get("hash") or "").lower(): t for t in ts}
        return self._list

    def torrent(self, torrent_hash: str) -> dict | None:
        """单个种子的视图；不在 qBittorrent 里返回 None。读不到抛 `ClaimsUnknown`。"""
        return self.torrents().get((torrent_hash or "").lower())

    def entries(self, torrent_hash: str) -> list[dict]:
        """种子的**全部**文件条目（含优先级 0）。404 = 种子已不在，返回 []。"""
        h = (torrent_hash or "").lower()
        if h not in self._files:
            if self.qbit is None:
                raise ClaimsUnknown("qBittorrent 不可用（登录失败或未配置）")
            try:
                self._files[h] = list(self.qbit.files(h) or [])
            except Exception as e:
                if not _is_404(e):
                    raise ClaimsUnknown(
                        f"files({h[:8]}) 读取失败：{type(e).__name__}: {e}") from e
                self._files[h] = []
        return self._files[h]

    # ---------------------------------------------------------------- 查询
    def check(self, target: str | Path, *, own_hash: str = "",
              own_path: str | Path | None = None, disk: bool = True,
              exempt: Iterable[str] = ()) -> ClaimCheck:
        """`target`（一个文件的绝对路径）此刻被谁占着。

        - `own_hash` / `own_path`：问的人自己。`own_path` 是它**此刻**的绝对路径（改名前）；
          盘上那一侧按 inode 豁免 `own_path` 与 `own_path.!qB`，qBittorrent 那一侧豁免
          `own_hash` 里路径（`fold` 后）等于 `own_path` 的那个条目。只给 `own_hash`
          不给 `own_path` 时，这个种子的所有条目都豁免。
        - `disk=False`：只问 qBittorrent（relink / 重加种子：目标文件本来就该在盘上）。
        - `exempt`：这些种子的条目不算占用（回退重加撞车受害者时，当初保留的那一方）。
        """
        target = Path(target)
        out = ClaimCheck(str(target))
        own = Path(own_path) if own_path else None
        try:
            if disk:
                mine = set()
                if own is not None:
                    for p in (own, Path(str(own) + PARTIAL)):
                        k = _lstat_key(p)
                        if k is not None:
                            mine.add(k)
                for key, (p, is_partial) in _disk_hits(target).items():
                    if key not in mine:
                        out.claimants.append(Claimant("disk", str(p), is_partial))
            out.claimants += self._qbit_claimants(target, own_hash, own, exempt)
        except ClaimsUnknown as e:
            out.unknown = str(e)
        return out

    def _qbit_claimants(self, target: Path, own_hash: str, own: Path | None,
                        exempt: Iterable[str]) -> list[Claimant]:
        tf = fold(target)
        keys = {tf: False, fold(str(target) + PARTIAL): True}
        own_h = (own_hash or "").lower()
        own_f = fold(own) if own is not None else None
        skip = {h.lower() for h in exempt} | {h.lower() for h in self.ignore}
        out = []
        for h, t in self.torrents().items():
            if h in skip:
                continue
            sp = (t.get("save_path") or "").rstrip("/")
            if not sp or not tf.startswith(fold(sp) + "/"):
                continue              # 条目 = save_path/名字，save_path 不是目标祖先就不可能撞上
            for e in self.entries(h):
                if e.get("priority", 1) == 0:
                    continue          # 不下载的条目 qBittorrent 不会往那写
                p = Path(sp) / e["name"]
                is_partial = keys.get(fold(p))
                if is_partial is None:
                    continue
                if h == own_h and (own_f is None or fold(p) == own_f):
                    continue          # 问的人自己的那个条目
                out.append(self._claimant(h, t, e, p, is_partial))
        return out

    @staticmethod
    def _claimant(h: str, t: dict, e: dict | None, p: Path | str,
                  partial: bool = False) -> Claimant:
        return Claimant("qbit", str(p), partial, hash=h, name=t.get("name", ""),
                        entry=(e or {}).get("name", ""), progress=t.get("progress"),
                        state=t.get("state", ""), category=t.get("category", ""))

    def check_dir(self, target_dir: str | Path, *, movers: Iterable[str] = ()) -> ClaimCheck:
        """`target_dir`（要搬进去的目录）此刻被谁占着。

        - 盘上：这个名字（`fold` 口径）已经存在——**不**豁免"只差大小写的自己"：
          目录改名只改大小写时 setLocation 在大小写不敏感的卷上改不动目录名，与以前
          `new.exists()` 在 APFS 上的行为一致，一律交给人。
        - qBittorrent：`movers` 之外的种子，save_path 在它之下（没有元数据的也算——
          元数据一到就往那写），或 save_path 在它之上、有条目落在它之下。
        """
        target_dir = Path(target_dir)
        out = ClaimCheck(str(target_dir))
        movers = {h.lower() for h in movers}
        try:
            for p, _ in _disk_hits(target_dir, partial=False).values():
                out.claimants.append(Claimant("disk", str(p)))
            fd = fold(target_dir)
            skip = movers | {h.lower() for h in self.ignore}
            for h, t in self.torrents().items():
                if h in skip:
                    continue
                sp = (t.get("save_path") or "").rstrip("/")
                if not sp:
                    continue
                fsp = fold(sp)
                if fsp == fd or fsp.startswith(fd + "/"):
                    out.claimants.append(self._claimant(h, t, None, sp))
                    continue
                if not fd.startswith(fsp + "/"):
                    continue
                for e in self.entries(h):
                    p = Path(sp) / e["name"]
                    if e.get("priority", 1) != 0 and fold(p).startswith(fd + "/"):
                        out.claimants.append(self._claimant(h, t, e, p))
                        break
        except ClaimsUnknown as e:
            out.unknown = str(e)
        return out
