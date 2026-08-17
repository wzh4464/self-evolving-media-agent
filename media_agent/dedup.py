"""内容去重：文件大小 + 头尾 8MB 哈希。

**铁律：绝不按文件名判重。** AutoBangumi 会改名，同一份内容在不同阶段文件名完全不同；
反过来不同来源的同一集（简/繁、720p/1080p、不同字幕组）文件名可能高度相似但内容不同。
只有内容哈希能给出可据以删除的证据。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .cache import Cache

CHUNK = 8 * 1024 * 1024


def content_digest(path: Path, cache: Cache | None = None) -> str | None:
    """返回 `size:md5(head8M+tail8M)`。读不到返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None

    if cache:
        hit = cache.get_hash(str(path), st.st_size, st.st_mtime)
        if hit:
            return hit

    try:
        with path.open("rb") as f:
            head = f.read(CHUNK)
            if st.st_size > CHUNK:
                f.seek(max(0, st.st_size - CHUNK))
                tail = f.read(CHUNK)
            else:
                tail = b""
    except OSError:
        return None

    digest = f"{st.st_size}:{hashlib.md5(head + tail).hexdigest()}"
    if cache:
        cache.put_hash(str(path), st.st_size, st.st_mtime, digest)
    return digest


def same_content(a: Path, b: Path, cache: Cache | None = None) -> bool:
    """两个文件是否字节级同一份内容（快速判定）。"""
    da, db = content_digest(a, cache), content_digest(b, cache)
    return bool(da and db and da == db)
