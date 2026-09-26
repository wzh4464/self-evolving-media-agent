""".torrent 文件与测试用磁盘文件的最小实现。

抓取路径（`_op_grab_episode`）要真的下一个 .torrent、算 v1 infohash
（`Executor._infohash_v1`）、再交给 qBittorrent。离线测试里这三步都得
是真的：infohash 算错，`_rename_grabbed` 就找不到刚加进去的种子，
抓取照样记 applied，只是改名悄悄没发生——正是 2026-09-26 那种
"下载没坏、记账坏了"的形态。所以这里给出与执行器同一口径的 bencode。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 测试文件的头部标记。FakeProbe 靠它认出"这是哪个文件"，
# 于是 renameFile / setLocation / 移进隔离区之后探测结果仍跟着文件走。
FAKE_MAGIC = b"MAFAKE:"
_HEADER_LEN = 64


def bencode(x) -> bytes:
    if isinstance(x, bool):
        raise TypeError("bencode 不支持 bool")
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode("utf-8")
    if isinstance(x, (bytes, bytearray)):
        return b"%d:%s" % (len(x), bytes(x))
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted((k.encode("utf-8") if isinstance(k, str) else k, v)
                       for k, v in x.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"bencode 不支持 {type(x).__name__}")


def bdecode(b: bytes, i: int = 0):
    """返回 (值, 下一个下标)。字符串一律保持 bytes，调用方自己 decode。"""
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i)
        return int(b[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while b[i:i + 1] != b"e":
            v, i = bdecode(b, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while b[i:i + 1] != b"e":
            k, i = bdecode(b, i)
            v, i = bdecode(b, i)
            out[k.decode("utf-8")] = v
        return out, i + 1
    j = b.index(b":", i)
    n = int(b[i:j])
    return b[j + 1:j + 1 + n], j + 1 + n


def make_torrent(name: str, files: dict[str, int] | None = None,
                 size: int | None = None) -> tuple[bytes, str]:
    """造一个 .torrent，返回 (blob, v1 infohash)。

    - 单文件种子：`make_torrent("x.mkv", size=600_000_000)`
    - 多文件种子：`make_torrent("[G] Show 01-02", files={"a.mkv": 1, "sub/b.ass": 2})`，
      `files` 的键是相对种子根目录的路径（根目录名就是 `name`）。
    """
    if files is None:
        info = {"name": name, "length": int(size or 1), "piece length": 16384,
                "pieces": b"\0" * 20}
    else:
        info = {"name": name, "piece length": 16384, "pieces": b"\0" * 20,
                "files": [{"path": p.split("/"), "length": int(s)}
                          for p, s in files.items()]}
    blob = bencode({"announce": "http://tracker.invalid/announce", "info": info})
    return blob, hashlib.sha1(bencode(info)).hexdigest()


def torrent_files(blob: bytes) -> tuple[str, str, dict[str, int], bool]:
    """解析 .torrent → (infohash, name, {相对路径: 字节数}, 是否多文件)。

    多文件时路径**不含**根目录名，由调用方按布局（Original / NoSubfolder）决定前缀。
    """
    meta, _ = bdecode(blob, 0)
    info = meta["info"]
    h = hashlib.sha1(bencode(info)).hexdigest()
    name = info["name"].decode("utf-8")
    if "files" in info:
        files = {"/".join(p.decode("utf-8") for p in f["path"]): int(f["length"])
                 for f in info["files"]}
        return h, name, files, True
    return h, name, {name: int(info["length"])}, False


def write_sparse(path: Path, size: int, ident: str) -> Path:
    """写一个稀疏文件：开头是带身份的小标记，其余是空洞，`stat` 报满额大小。

    - 创建是瞬时的（APFS 与 ext4 都支持稀疏文件），几百 MB 的"正片"不占磁盘。
    - 扫描的归属打分拿"磁盘大小 == 种子声明大小"判断谁是真主人
      （scan.py 来源 1），所以大小必须是字节级精确的逻辑大小。
    - 标记让 `content_digest` 对不同文件给出不同摘要、对 `same_content_as`
      的副本给出相同摘要，也让 FakeProbe 认得出文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (FAKE_MAGIC + ident.encode("utf-8") + b"\n")[:_HEADER_LEN]
    with path.open("wb") as fh:
        fh.write(header[:max(size, 0)])
        fh.truncate(max(size, 0))
    return path


def read_ident(path: Path) -> str | None:
    """读出 `write_sparse` 写下的身份；不是测试文件返回 None。"""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(_HEADER_LEN)
    except OSError:
        return None
    if not head.startswith(FAKE_MAGIC):
        return None
    return head[len(FAKE_MAGIC):].split(b"\n", 1)[0].decode("utf-8", "replace")
