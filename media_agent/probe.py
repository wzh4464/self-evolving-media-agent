"""媒体文件探测：时长、编码、字幕轨。**打开文件看事实，不猜文件名。**

**为什么需要它**：择源和判重这两处都在回答"哪个版本更好"，但两处都只看
发布标题。`preferences.py` 自己的文档写着"真正的验证只能等下载完 ffprobe"，
而下载完之后并没有人去 ffprobe——`DuplicateEpisodeDetector` 排序时用的还是
`parse_quality(文件名)`。

代价 2026-09-05 付过：尼古喵喵 S01E08 同时有两个候选

    [Dynamis One] … (ABEMA 1920x1080 AVC AAC).mkv      710MB  零字幕轨
    [LoliHouse] … [WebRip 1080p HEVC-10bit AAC SRTx2]  566MB  简繁双字幕轨

`parse_quality` 从名字里读不出 LoliHouse 那份带字幕（"简繁内封字幕"只出现在
Mikan 的站点标题里，种子内部文件名写的是 `SRTx2`），两者同判 `简=False`
并列，最后落到体积——AVC 生肉 710MB 就赢了 HEVC 带双字幕的 566MB。
留下的那份一条中文字幕都没有。

而真相一直在文件里：一个有 2 条 subrip 轨，一个 0 条。

**探测是有代价的**，一次 ffprobe 几十毫秒、跨网络存储更慢，所以带缓存：
按 (路径, 大小, mtime) 做键，文件没变就不重复探。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .naming import looks_chinese, looks_simplified, looks_traditional

_FFPROBE = ("/opt/homebrew/bin/ffprobe", "ffprobe")
_FFMPEG = ("/opt/homebrew/bin/ffmpeg", "ffmpeg")



@dataclass(frozen=True)
class MediaInfo:
    """一个媒体文件的客观事实。取不到的字段为 None / 空。"""
    duration: float | None = None
    vcodec: str = ""
    height: int = 0
    sub_count: int = 0
    sub_marks: tuple = field(default_factory=tuple)   # 各字幕轨的 language+title

    @property
    def has_simplified(self) -> bool:
        return any(looks_simplified(m) for m in self.sub_marks)

    @property
    def has_traditional(self) -> bool:
        return any(looks_traditional(m) for m in self.sub_marks)

    @property
    def has_chinese(self) -> bool:
        return any(looks_chinese(m) for m in self.sub_marks)

    # 字幕能力分档。**探到的内封轨要压过靠名字猜的内嵌**——用户口径是
    # 「内封最好，内嵌也行」，两者同分就等于把这个偏好抹平了。
    # 2026-09-08 穹庐下的魔女 S01E11 正是这么丢的：探到简繁双内封轨的那份
    # 只拿到和「名字里写着 CHT、疑似内嵌」同样的分，打平后按体积输掉。
    SOFT_SIMPLIFIED = 5
    SOFT_CHINESE = 4
    HARD_SIMPLIFIED = 3
    HARD_CHINESE = 2
    SOFT_OTHER = 1
    NONE = 0

    def subtitle_rank(self) -> int:
        """按**实际探到的内封字幕轨**打分。

        0 分只代表"没有内封字幕轨"，**不代表没字幕**——内嵌硬字幕在容器里
        看不见。所以调用方要用文件名证据把它抬到 HARD_* 档，而不是判死刑。
        """
        if self.has_simplified:
            return self.SOFT_SIMPLIFIED
        if self.has_chinese:
            return self.SOFT_CHINESE
        return self.SOFT_OTHER if self.sub_count else self.NONE


_CACHE: dict = {}


def _run(exes, args, timeout=30):
    for exe in exes:
        try:
            r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
            return r
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def probe(path: Path) -> MediaInfo | None:
    """探测一个文件；探不到（文件不存在 / 没有 ffprobe）返回 None。

    结果按 (路径, 大小, mtime) 缓存在进程内——同一轮 run 里判重和择源
    会反复问同一个文件。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, int(st.st_mtime))
    if key in _CACHE:
        return _CACHE[key]

    r = _run(_FFPROBE, ["-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name,height:"
                        "stream_tags=language,title",
                        "-of", "json", str(path)])
    if r is None or r.returncode != 0 or not r.stdout.strip():
        _CACHE[key] = None
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        _CACHE[key] = None
        return None

    dur = None
    try:
        dur = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    vcodec, height, marks = "", 0, []
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not vcodec:
            vcodec = s.get("codec_name") or ""
            height = int(s.get("height") or 0)
        elif s.get("codec_type") == "subtitle":
            tags = s.get("tags") or {}
            marks.append("%s %s" % (tags.get("language") or "", tags.get("title") or ""))
    info = MediaInfo(duration=dur, vcodec=vcodec, height=height,
                     sub_count=len(marks), sub_marks=tuple(marks))
    _CACHE[key] = info
    return info


def duration(path: Path) -> float | None:
    """时长（秒）。取不到返回 None。"""
    info = probe(path)
    return info.duration if info else None


def tail_decodes(path: Path, dur: float | None) -> bool:
    """尾部能否真正解出画面。

    截断文件最阴险的一种：容器头里写着完整时长，`duration()` 读出来一切正常，
    但后半段数据根本不存在。只有真去解码末尾才能戳穿。
    从 `dur - 8s` 起解 1 帧，解不出就当它是残的。
    """
    if dur is None or dur < 12:
        return True                      # 太短的（菜单、PV）不适用这条，不拦
    ss = max(0.0, dur - 8.0)
    r = _run(_FFMPEG, ["-v", "error", "-ss", "%.2f" % ss, "-i", str(path),
                       "-frames:v", "1", "-f", "null", "-"], timeout=60)
    if r is None:
        return True                      # 没有 ffmpeg 就别拿它当否定证据
    return r.returncode == 0 and not r.stderr.strip()


# 跨编码比体积是没意义的：同画质下 HEVC/AV1 大约只要 AVC 的六成。
# 2026-09-05 就是靠裸体积把 AVC 生肉判赢了 HEVC 带字幕版。
_CODEC_WEIGHT = {"hevc": 1.6, "h265": 1.6, "av1": 1.8, "vp9": 1.5}


def size_for_compare(size: int, vcodec: str) -> float:
    """把体积折算成"等效 AVC 体积"，好让跨编码的比较有意义。"""
    return size * _CODEC_WEIGHT.get((vcodec or "").lower(), 1.0)
