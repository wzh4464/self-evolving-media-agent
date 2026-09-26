"""FakeProbe：在 `media_agent.probe._run` 这一个口子上假装是 ffprobe / ffmpeg。

为什么是 `_run` 而不是 `probe()`：`builtin.py` 用 `from ..probe import probe`
按名字导入，`purge.py` 按名字导入 `duration` / `tail_decodes`——替换
`media_agent.probe.probe` 根本传不到它们。`_run` 是三者共同的、在调用时
才查找的唯一子进程出口。

它合成的是 **ffprobe 的 JSON 输出**，于是 probe.py 里真正的解析逻辑
（取 duration、第一条视频流、各字幕轨 language+title）照样被执行。

**默认自动启用**：开发机上 `/opt/homebrew/bin/ffprobe` 真实存在，
不拦的话测试会对稀疏文件跑真 ffprobe，结果随机器而变。

文件身份按 `write_sparse` 写进文件头的标记认，不按路径认——
所以改名、搬目录、移进隔离区之后，探测结果仍然跟着文件走。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from media_agent import probe as probe_mod

from .torrentfile import read_ident


@dataclass
class ProbeSpec:
    vcodec: str = "h264"
    height: int = 1080
    duration: float | None = 1420.0
    subs: list[tuple[str, str]] = field(default_factory=list)   # [(language, title)]
    truncated: bool = False          # 尾部解不出帧（ffmpeg 报错）

    def ffprobe_json(self) -> str:
        streams = [{"codec_type": "video", "codec_name": self.vcodec, "height": self.height}]
        streams += [{"codec_type": "audio", "codec_name": "aac"}]
        streams += [{"codec_type": "subtitle", "codec_name": "ass",
                     "tags": {"language": lang, "title": title}}
                    for lang, title in self.subs]
        fmt = {} if self.duration is None else {"duration": f"{self.duration:.6f}"}
        return json.dumps({"format": fmt, "streams": streams})


def video(vcodec: str = "h264", height: int = 1080, duration: float | None = 1420.0,
          subs: list | None = None, truncated: bool = False) -> ProbeSpec:
    """`subs` 可写成 `[("chi", "简体中文")]` 或 `["chi 简体中文"]`。"""
    pairs = []
    for s in subs or []:
        if isinstance(s, str):
            lang, _, title = s.partition(" ")
            pairs.append((lang, title))
        else:
            pairs.append(tuple(s))
    return ProbeSpec(vcodec=vcodec, height=height, duration=duration, subs=pairs,
                     truncated=truncated)


class FakeProbe:
    def __init__(self):
        self._by_ident: dict[str, ProbeSpec] = {}
        self._by_path: dict[str, ProbeSpec] = {}
        self.calls: list[tuple[str, str]] = []      # (ffprobe|ffmpeg, path)

    # ---- 登记 ----
    def register(self, ident: str, spec: ProbeSpec) -> None:
        """按文件身份登记（LibraryBuilder 用）。"""
        self._by_ident[ident] = spec
        probe_mod._CACHE.clear()

    def set(self, path: str | Path, spec: ProbeSpec | None = None, **kw) -> None:
        """按路径登记（给不是 builder 造出来的文件用）。`kw` 同 `video()`。"""
        self._by_path[str(path)] = spec or video(**kw)
        probe_mod._CACHE.clear()

    def spec_for(self, path: str) -> ProbeSpec | None:
        spec = self._by_path.get(str(path))
        if spec is None:
            ident = read_ident(Path(path))
            if ident is not None:
                spec = self._by_ident.get(ident)
        return spec

    # ---- 替身 ----
    def run(self, exes, args, timeout=30):
        """签名与 `probe._run(exes, args, timeout)` 一致。"""
        if exes is probe_mod._FFMPEG:
            path = args[args.index("-i") + 1]
            self.calls.append(("ffmpeg", path))
            spec = self.spec_for(path)
            if spec is not None and spec.truncated:
                return subprocess.CompletedProcess(
                    ["ffmpeg", *args], 1, "", "Invalid data found when processing input")
            return subprocess.CompletedProcess(["ffmpeg", *args], 0, "", "")
        path = args[-1]
        self.calls.append(("ffprobe", path))
        spec = self.spec_for(path)
        if spec is None:
            # 与"文件不是媒体 / 没装 ffprobe"同形：probe() 返回 None
            return subprocess.CompletedProcess(["ffprobe", *args], 1, "",
                                               f"{path}: Invalid data found")
        return subprocess.CompletedProcess(["ffprobe", *args], 0, spec.ffprobe_json(), "")
