"""FakeQbit：`QBitClient` 在本项目里用到的那 13 个方法，背后是临时目录里的真文件。

语义以生产机 qBittorrent v5.2.3 / libtorrent 2.0.13 的实测为准
（2026-09-26 只读核对了 539 个种子，见 AGENTS.md 第 2、3 条）：

- `renameFile` **不改**种子的 `name`（显示名），只改文件条目与磁盘文件，
  `.!qB` 半成品一起改。源文件不在盘上时只改映射（`relink_torrent` 依赖这一点）。
- `content_path`：单文件 = `save_path/<文件>`；Original 布局（所有文件共享一个
  根目录）= `save_path/<根>`；NoSubfolder 多文件 = `save_path` 本身。
  最后这条是 B1（死种把整个 Season 目录送进隔离区）的前提，必须保真。
- `tags` 排序后用 `", "` 连接；`progress` 完成时是 1。
- `files()` 对未知 hash 抛 `QBitError("torrents/files -> HTTP 404: Not Found")`，
  与真客户端的报错类型、文本一致（B2 就是这个 404）。
- `set_file_priority(…, 0)` 只改优先级，文件留在盘上（`use_unwanted_folder=False`）。
- `delete(delete_files=False)` 只摘记录；未知 hash 静默忽略（真 API 也是 200）。

没建模的方法一律报 tripwire（`unmodeled`）再抛 AttributeError——代码开始调用
新接口时，测试会立刻告诉你假对象该补了，而不是被某个 `except` 吞成空结果。
"""
from __future__ import annotations

import copy
import os
import re
import time
from pathlib import Path

import httpx

from media_agent.clients import QBitError

from .torrentfile import torrent_files

_BTIH = re.compile(r"xt=urn:btih:([0-9A-Za-z]+)")


class _Fault:
    def __init__(self, method: str, torrent_hash: str | None, exc: BaseException,
                 times: int | None):
        self.method, self.hash, self.exc, self.times = method, torrent_hash, exc, times


def _qb_path_ok(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and ".." not in Path(rel).parts


class FakeQbit:
    """见模块文档。构造参数：

    - `tripwire`：可选，传入后 404/409、覆盖写、未建模调用都会记事件。
    - `default_save_path`：`add_torrent` 没给 `save_path` 时落到哪（qBit 的全局默认）。
    - `rename_collision`：`"overwrite"`（默认，libtorrent 在 POSIX 上用 rename(2)，
      **未经生产实测**，所以每次覆盖都记 `qbit_overwrite`）或 `"raise"`（报 409）。
    - `async_moves`：True 时 `set_location` 只登记，`drain()` 才真正搬文件——
      近似 qBittorrent 的异步 moveStorage，用来暴露"setLocation 后立刻 _merge_tree"的竞态。
    """

    def __init__(self, tripwire=None, *, default_save_path: str | Path = "",
                 rename_collision: str = "overwrite", async_moves: bool = False):
        assert rename_collision in ("overwrite", "raise")
        self.tripwire = tripwire
        self.default_save_path = str(default_save_path)
        self.rename_collision = rename_collision
        self.async_moves = async_moves
        self._t: dict[str, dict] = {}
        self._catalog: dict[str, dict] = {}     # hash → {"name", "files", "multi"}：磁力重加时恢复元数据
        self._faults: list[_Fault] = []
        self._pending_moves: list[tuple[str, str]] = []
        self.categories: set[str] = set()
        self.calls: list[tuple] = []            # 所有 API 调用的有序记录
        self.overwrites: list[tuple[str, str]] = []

    # ================================================================ 内部工具
    def _trip(self, kind: str, detail: str) -> None:
        if self.tripwire is not None:
            self.tripwire.record(kind, detail)

    def _err(self, status: int, path: str, text: str) -> QBitError:
        """与 `QBitClient._get/_post` 同一格式的错误，并记 tripwire。"""
        e = QBitError(f"{path} -> HTTP {status}: {text}")
        self._trip("qbit_error", str(e))
        return e

    def _maybe_fail(self, method: str, torrent_hash: str | None = None) -> None:
        for f in list(self._faults):
            if f.method != method or (f.hash is not None and f.hash != torrent_hash):
                continue
            if f.times is not None:
                f.times -= 1
                if f.times <= 0:
                    self._faults.remove(f)
            raise f.exc

    def _get(self, h: str, path: str) -> dict:
        t = self._t.get(h)
        if t is None:
            raise self._err(404, path, "Not Found")
        return t

    @staticmethod
    def _norm_tags(tags) -> set[str]:
        if isinstance(tags, (set, list, tuple)):
            items = tags
        else:
            items = (tags or "").split(",")
        return {x.strip() for x in items if x and x.strip()}

    def _move(self, src: Path, dst: Path) -> None:
        """libtorrent 式的搬文件：目标已存在就按 `rename_collision` 处理。"""
        if not src.exists():
            return
        if dst.exists() and not os.path.samefile(src, dst):
            if self.rename_collision == "raise":
                raise self._err(409, "torrents/renameFile",
                                f"目标已存在: {dst.name}")
            self.overwrites.append((str(src), str(dst)))
            self._trip("qbit_overwrite", f"{src} -> {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)

    @staticmethod
    def _content_path(t: dict) -> str:
        sp = Path(t["save_path"])
        fs = t["_files"]
        if not fs:
            return str(sp)                        # 没有元数据：qBit 回落到存储位置
        if len(fs) == 1:
            return str(sp / fs[0]["name"])
        parts = [f["name"].split("/") for f in fs]
        if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1:
            return str(sp / parts[0][0])          # Original 布局：根目录
        return str(sp)                            # NoSubfolder：就是 save_path

    def _root_path(self, t: dict) -> str:
        cp = self._content_path(t)
        if len(t["_files"]) > 1 and cp != str(Path(t["save_path"])):
            return cp
        return ""

    def _view(self, t: dict) -> dict:
        fs = t["_files"]
        wanted = sum(f["size"] for f in fs if f["priority"] != 0)
        prog = t["progress"]
        return {
            "hash": t["hash"], "infohash_v1": t["hash"], "name": t["name"],
            "save_path": t["save_path"], "content_path": self._content_path(t),
            "root_path": self._root_path(t), "download_path": "",
            "category": t["category"], "tags": ", ".join(sorted(t["tags"])),
            "state": t["state"], "progress": 1 if prog >= 1 else prog,
            "added_on": t["added_on"], "last_activity": t["last_activity"],
            "seen_complete": t["seen_complete"], "num_seeds": t["num_seeds"],
            "num_complete": t["num_complete"], "num_leechs": 0,
            "num_incomplete": t["num_incomplete"], "availability": t["availability"],
            "magnet_uri": t["magnet_uri"], "size": wanted,
            "total_size": sum(f["size"] for f in fs),
            "amount_left": int(wanted * (1 - min(prog, 1))), "auto_tmm": False,
        }

    def _files_view(self, t: dict) -> list[dict]:
        out = []
        for f in t["_files"]:
            p = f["progress"]
            out.append({"index": f["index"], "name": f["name"], "size": f["size"],
                        "progress": 1 if p >= 1 else p, "priority": f["priority"],
                        "is_seed": p >= 1, "piece_range": [0, 0],
                        "availability": t["availability"]})
        return out

    def _disk_progress(self, t: dict) -> float:
        """按磁盘上真实存在、且大小对得上的字节算进度（recheck / 加种时的检查）。"""
        total = sum(f["size"] for f in t["_files"] if f["priority"] != 0)
        if not total:
            return 0.0
        have = 0
        sp = Path(t["save_path"])
        for f in t["_files"]:
            if f["priority"] == 0:
                continue
            p = sp / f["name"]
            try:
                if p.stat().st_size == f["size"]:
                    have += f["size"]
            except OSError:
                pass
        return have / total

    def _set_progress(self, t: dict, progress: float, paused: bool = False) -> None:
        t["progress"] = progress
        for f in t["_files"]:
            f["progress"] = progress
        if progress >= 1:
            t["state"] = "stoppedUP" if paused else "stalledUP"
        elif not t["_files"]:
            t["state"] = "stoppedDL" if paused else "metaDL"
        else:
            t["state"] = "stoppedDL" if paused else "downloading"

    # ================================================================ 构造（给 LibraryBuilder 用）
    def seed(self, torrent_hash: str, *, name: str, save_path: str | Path,
             files: dict[str, int], progress: float = 1.0, state: str | None = None,
             category: str = "", tags="", added_on: float | None = None,
             num_seeds: int = 0, num_complete: int | None = None,
             availability: float | None = None, priorities: dict[str, int] | None = None,
             magnet_uri: str | None = None, last_activity: float | None = None,
             seen_complete: float | None = None) -> dict:
        """直接放一个种子进来（不碰磁盘；磁盘由调用方负责写）。返回内部记录。

        `last_activity` / `seen_complete` 是 Unix 时间戳（WebAPI 文档的语义；
        **未经生产逐条实测**）。默认：`last_activity` = 加入时间（从没传过数据时
        qBittorrent 报的就是加入时间），`seen_complete` = 已完成的取加入时间、
        未完成的取 0（从未见过完整副本）。
        """
        h = torrent_hash.lower()
        assert h not in self._t, f"hash 重复: {h}"
        pri = priorities or {}
        t = {
            "hash": h, "name": name, "save_path": str(Path(save_path)),
            "category": category, "tags": self._norm_tags(tags),
            "progress": progress,
            "state": state or ("stalledUP" if progress >= 1 else "downloading"),
            "added_on": int(added_on if added_on is not None else time.time() - 86400),
            "last_activity": None if last_activity is None else int(last_activity),
            "seen_complete": None if seen_complete is None else int(seen_complete),
            "num_seeds": num_seeds,
            "num_complete": (5 if progress >= 1 else 0) if num_complete is None else num_complete,
            "num_incomplete": 0,
            "availability": (-1 if progress >= 1 else 0) if availability is None else availability,
            "magnet_uri": magnet_uri or f"magnet:?xt=urn:btih:{h}",
            "_files": [{"index": i, "name": n, "size": int(s), "priority": pri.get(n, 1),
                        "progress": progress}
                       for i, (n, s) in enumerate(files.items())],
        }
        if t["last_activity"] is None:
            t["last_activity"] = t["added_on"]
        if t["seen_complete"] is None:
            t["seen_complete"] = t["added_on"] if progress >= 1 else 0
        self._t[h] = t
        self._catalog.setdefault(h, self._intrinsic(name, files))
        if category:
            self.categories.add(category)
        return t

    @staticmethod
    def _intrinsic(name: str, files: dict[str, int]) -> dict:
        """从"当前文件条目"反推 .torrent 里的 info（name + 相对根目录的文件）。

        磁力重加（rollback 的 `readd_torrent`）拿回的是**种子本身**的元数据，
        不是被 renameFile 改过的条目：单文件种子会以原发布名回来，
        NoSubfolder 种子按 `no_subfolder=False` 重加会长出根目录。这正是真实
        qBittorrent 的行为，所以要按 info 记，而不是按当前条目记。
        """
        names = list(files)
        if len(names) == 1 and "/" not in names[0]:
            orig = name if Path(name).suffix.lower() == Path(names[0]).suffix.lower() else names[0]
            return {"name": orig, "files": {orig: files[names[0]]}, "multi": False}
        parts = [n.split("/") for n in names]
        if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1:
            root = parts[0][0]
            return {"name": root, "multi": True,
                    "files": {"/".join(p[1:]): files[n] for p, n in zip(parts, names, strict=True)}}
        return {"name": name, "files": dict(files), "multi": True}

    def raw(self, torrent_hash: str) -> dict:
        """内部记录（可直接改字段来造状态，比如 availability / num_complete）。"""
        return self._t[torrent_hash.lower()]

    def has(self, torrent_hash: str) -> bool:
        return torrent_hash.lower() in self._t

    def torrent(self, torrent_hash: str) -> dict:
        """与 `torrents()` 同形的单个视图；不记调用、不触发故障注入。"""
        return self._view(self._t[torrent_hash.lower()])

    def file_names(self, torrent_hash: str) -> list[str]:
        return [f["name"] for f in self._t[torrent_hash.lower()]["_files"]]

    def complete(self, torrent_hash: str) -> None:
        """把种子"下完"：半成品改回正名、补齐大小，进度 1。"""
        from .torrentfile import write_sparse
        t = self._t[torrent_hash.lower()]
        sp = Path(t["save_path"])
        for f in t["_files"]:
            p = sp / f["name"]
            part = Path(str(p) + ".!qB")
            if part.exists():
                os.replace(part, p)
            if not p.exists() or p.stat().st_size != f["size"]:
                write_sparse(p, f["size"], f"{t['hash']}:{f['index']}")
        self._set_progress(t, 1.0)

    def fail(self, method: str, *, hash: str | None = None,
             exc: BaseException | None = None, times: int | None = 1) -> None:
        """故障注入：下 `times` 次（None = 一直）调用 `method`（可限定某个 hash）时抛 `exc`。

        默认异常是 `httpx.ReadTimeout`——生产上 qBit WebUI 超时就是这个形态
        （2026-09-19/20 三次登录超时，run.err.log）。
        """
        self._faults.append(_Fault(method, hash.lower() if hash else None,
                                   exc or httpx.ReadTimeout("timed out (injected)"), times))

    def drain(self) -> None:
        """`async_moves=True` 时，完成所有挂起的 setLocation。"""
        pending, self._pending_moves = self._pending_moves, []
        for h, loc in pending:
            if h in self._t:
                self._do_move(self._t[h], loc)

    def snapshot(self) -> dict:
        """规范化的全量状态，便于 `assert before == after`。"""
        return {h: {"name": t["name"], "save_path": t["save_path"],
                    "content_path": self._content_path(t), "category": t["category"],
                    "tags": ", ".join(sorted(t["tags"])), "state": t["state"],
                    "progress": t["progress"],
                    "files": [(f["name"], f["size"], f["priority"]) for f in t["_files"]]}
                for h, t in sorted(self._t.items())}

    # ================================================================ QBitClient 接口
    def torrents(self, category: str | None = None) -> list[dict]:
        self.calls.append(("torrents", category))
        self._maybe_fail("torrents")
        return [copy.deepcopy(self._view(t)) for t in self._t.values()
                if category is None or t["category"] == category]

    def files(self, torrent_hash: str) -> list[dict]:
        h = (torrent_hash or "").lower()
        self.calls.append(("files", h))
        self._maybe_fail("files", h)
        return copy.deepcopy(self._files_view(self._get(h, "torrents/files")))

    def add_torrent(self, source: bytes | str, *, save_path: str = "",
                    category: str = "", tags: str = "", paused: bool = False,
                    no_subfolder: bool = True, **extra) -> bool:
        self.calls.append(("add_torrent", save_path, category, tags, paused,
                           no_subfolder, dict(extra)))
        self._maybe_fail("add_torrent")
        sp = save_path or self.default_save_path
        if not sp:
            raise self._err(400, "torrents/add", "no save path")

        if isinstance(source, (bytes, bytearray)):
            h, name, rel_files, multi = torrent_files(bytes(source))
            self._catalog.setdefault(h, {"name": name, "files": rel_files, "multi": multi})
        elif isinstance(source, str) and source.startswith("magnet:"):
            m = _BTIH.search(source)
            if not m:
                raise self._err(415, "torrents/add", "bad magnet")
            h = m.group(1).lower()
            cat = self._catalog.get(h)
            name = cat["name"] if cat else h
            rel_files = cat["files"] if cat else {}
            multi = cat["multi"] if cat else False
        else:
            self._trip("unmodeled", f"FakeQbit.add_torrent(url={source!r})")
            raise AttributeError("FakeQbit 只建模了 .torrent bytes 与 magnet 两种来源")

        if h in self._t:
            return False                           # 真 API：409 → QBitClient 返回 False

        if multi and not no_subfolder:
            files = {f"{name}/{rel}": s for rel, s in rel_files.items()}   # Original：带根目录
        else:
            files = dict(rel_files)                # NoSubfolder 剥掉根目录；单文件本来就没有
        t = self.seed(h, name=name, save_path=sp, files=files, progress=0.0,
                      category=category, tags=tags, added_on=time.time(),
                      num_complete=0, availability=0,
                      magnet_uri=f"magnet:?xt=urn:btih:{h}&dn={name}")
        # libtorrent 加种时会检查已存在的文件（没有 resume data 就查盘）
        self._set_progress(t, self._disk_progress(t) if files else 0.0, paused=paused)
        return True

    def rename_torrent(self, torrent_hash: str, name: str) -> None:
        h = torrent_hash.lower()
        self.calls.append(("rename_torrent", h, name))
        self._maybe_fail("rename_torrent", h)
        self._get(h, "torrents/rename")["name"] = name

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        h = torrent_hash.lower()
        self.calls.append(("rename_file", h, old_path, new_path))
        self._maybe_fail("rename_file", h)
        t = self._get(h, "torrents/renameFile")
        entry = next((f for f in t["_files"] if f["name"] == old_path), None)
        if (entry is None or not _qb_path_ok(new_path)
                or any(f["name"] == new_path and f is not entry for f in t["_files"])):
            raise self._err(409, "torrents/renameFile",
                            "Invalid newPath or oldPath, or newPath already in use")
        sp = Path(t["save_path"])
        src, dst = sp / old_path, sp / new_path
        self._move(src, dst)
        self._move(Path(str(src) + ".!qB"), Path(str(dst) + ".!qB"))
        entry["name"] = new_path                   # 注意：t["name"] 不变

    def _do_move(self, t: dict, location: str) -> None:
        old = Path(t["save_path"])
        new = Path(location)
        for f in t["_files"]:
            for suffix in ("", ".!qB"):
                self._move(Path(str(old / f["name"]) + suffix),
                           Path(str(new / f["name"]) + suffix))
        new.mkdir(parents=True, exist_ok=True)
        t["save_path"] = str(new)

    def set_location(self, hashes: list[str], location: str) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("set_location", tuple(hs), location))
        for h in hs:
            self._maybe_fail("set_location", h)
            t = self._t.get(h)
            if t is None:
                continue                            # 真 API 忽略未知 hash
            if self.async_moves:
                self._pending_moves.append((h, location))
            else:
                self._do_move(t, location)

    def set_category(self, hashes: list[str], category: str) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("set_category", tuple(hs), category))
        for h in hs:
            self._maybe_fail("set_category", h)
        if category:
            self.categories.add(category)           # 真客户端先 createCategory
        for h in hs:
            if h in self._t:
                self._t[h]["category"] = category

    def remove_categories(self, categories: list[str]) -> None:
        self.calls.append(("remove_categories", tuple(categories)))
        self._maybe_fail("remove_categories")
        for t in self._t.values():
            if t["category"] in categories:
                t["category"] = ""                  # 分类没了，种子变"无分类"而非被删
        self.categories -= set(categories)

    def add_tags(self, hashes: list[str], tags: str) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("add_tags", tuple(hs), tags))
        for h in hs:
            self._maybe_fail("add_tags", h)
            if h in self._t:
                self._t[h]["tags"] |= self._norm_tags(tags)

    def remove_tags(self, hashes: list[str], tags: str) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("remove_tags", tuple(hs), tags))
        for h in hs:
            self._maybe_fail("remove_tags", h)
            if h in self._t:
                self._t[h]["tags"] -= self._norm_tags(tags)

    def set_file_priority(self, torrent_hash: str, ids: list[int], priority: int) -> None:
        h = torrent_hash.lower()
        self.calls.append(("set_file_priority", h, tuple(ids), priority))
        self._maybe_fail("set_file_priority", h)
        t = self._get(h, "torrents/filePrio")
        idx = {f["index"]: f for f in t["_files"]}
        if not ids or any(i not in idx for i in ids):
            raise self._err(409, "torrents/filePrio", "File IDs are not valid")
        for i in ids:
            idx[i]["priority"] = int(priority)     # use_unwanted_folder=False：文件留在原地

    def recheck(self, hashes: list[str]) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("recheck", tuple(hs)))
        for h in hs:
            self._maybe_fail("recheck", h)
            t = self._t.get(h)
            if t is not None:
                self._set_progress(t, self._disk_progress(t))

    def delete(self, hashes: list[str], delete_files: bool) -> None:
        hs = [h.lower() for h in hashes]
        self.calls.append(("delete", tuple(hs), bool(delete_files)))
        for h in hs:
            self._maybe_fail("delete", h)
            t = self._t.pop(h, None)
            if t is None or not delete_files:
                continue
            sp = Path(t["save_path"])
            for f in t["_files"]:
                for suffix in ("", ".!qB"):
                    Path(str(sp / f["name"]) + suffix).unlink(missing_ok=True)
            root = self._root_path(t)
            if root:
                for d in sorted((p for p in Path(root).rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                try:
                    Path(root).rmdir()
                except OSError:
                    pass

    # ================================================================ 未建模
    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        self._trip("unmodeled", f"FakeQbit.{name}")
        raise AttributeError(f"FakeQbit.{name} 未建模——代码开始用新接口了，先给假对象补上")
