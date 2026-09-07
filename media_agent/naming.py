"""命名规则：解析集号、归一化比较、画质排序、目标文件名生成。

这里的规则全部来自实际踩过的坑，改动前先看 Notes/lessons.md。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".flv", ".wmv"}
SUB_EXTS = {".ass", ".srt", ".ssa", ".sub", ".sup", ".vtt"}

# 特典/周边：TMDB 不收录这些为 episode，放进库里只会污染刮削。
#
# **这些标记必须带编号**（`IN01` 而不是裸 `IN`）。教训：原先写的是
# `\bIN\d*\b`，`\d*` 允许零个数字，再叠上 IGNORECASE，于是它匹配英文介词
# "in"——《The Ghost **in** the Shell》整部番的每一集都被判成特典。
# 同类地雷还有 Made **in** Abyss、In the Land of Leadale…… 这条规则潜伏了很久，
# 直到抓了攻壳机动队才引爆：刚下的正片当场被 trash 进隔离区，而日志里
# 显示的是一次成功的"清理特典"。
#
# 两个字母的缩写（IN/PV/CM/SP）一律要求 `\d+`；语义明确的整词（NCOP/NCED/
# trailer/menu）才允许不带编号。
EXTRA_MARKERS = [
    r"\bmenu\d*\b", r"\bNCOP\d*\b", r"\bNCED\d*\b",
    r"\bIN\d+\b", r"\bPV\d+\b", r"\bCM\d+\b", r"\bSP\d+\b",
    # 裸缩写只在"独占一个方括号"时才算标记——`[SP]` `[CM]` 是特典，
    # 而正文里的 in/cm/sp 不是。PV 没有常见英文同形词，可以放宽。
    r"\bPV\b(?!\w)", r"\b(?:SP|CM|IN)\b(?=\s*[\]\)])",
    r"\btrailer\b", r"\bpreview\b",
    r"\bweb\s*preview\b", r"特典", r"tokuten", r"映像特典", r"菜单",
    r"\bBDMenu\b", r"\bcreditless\b",
]
_EXTRA_RE = re.compile("|".join(EXTRA_MARKERS), re.IGNORECASE)

# 解析集号时必须排除的干扰项（分辨率/编码/年份/音轨等）
_NOISE_RE = re.compile(
    r"\b(?:19|20)\d{2}\b"          # 年份
    r"|\b\d{3,4}[pP]\b"            # 1080p / 720P
    r"|\bx?26[45]\b|\bHEVC\b|\bAVC\b|\bH\.?26[45]\b"
    r"|\b10\s*bit\b|\b8\s*bit\b|\bMa10p\b|\byuv420p\d*\b"
    r"|\bAAC\d*\b|\bFLAC\b|\bDDP?\d?\.?\d?\b|\bOpus\b"
    r"|\b\d{3,4}x\d{3,4}\b"        # 1920x1080
    r"|\bv\d\b",                   # v2 修正版
    re.IGNORECASE,
)


def strip_noise(name: str) -> str:
    return _NOISE_RE.sub(" ", name)


def normalize(s: str) -> str:
    """归一化用于比较：全角→半角、大小写、空白折叠。

    踩过的坑：`Re：从零` vs `Re:从零`、`GNOSIA` vs `Gnosia`、`异世界四重奏S03E03`
    vs `异世界四重奏 S03E03` 都曾被误判成"未改名"。
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_extra(filename: str) -> bool:
    """是否为特典/菜单/PV 等非正片内容。"""
    return bool(_EXTRA_RE.search(filename))


def parse_episode(raw: str) -> tuple[int | None, int | None]:
    """从原始文件名解析 (season, episode)。season 为 None 表示未标注。

    覆盖实测过的所有命名风格；返回的 episode 可能是绝对集号，
    需要再用 bangumi.episode_offset 换算成季内集号。
    """
    base = raw.rsplit("/", 1)[-1]

    # 1) 显式 SxxExx —— 最可靠
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", base)
    if m:
        return int(m.group(1)), int(m.group(2))

    cleaned = strip_noise(base)

    # 2) `Sxx - 12` / `第二季 - 12`
    m = re.search(r"[Ss](\d{1,2})\s*[-–]\s*(\d{1,3})(?!\d)", cleaned)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 3) ` - 12 ` （LoliHouse / Dynamis One 风格），后面常跟 [ 或 (
    m = re.search(r"[-–]\s*(\d{1,3})(?:v\d)?\s*(?=[\[\(【]|$)", cleaned)
    if m:
        return None, int(m.group(1))

    # 4) `][08][` / `[17][` （喵萌 / DBD-Raws / Sakurato 风格）
    for m in re.finditer(r"[\[【]\s*(\d{1,3})(?:v\d)?\s*[\]】]", cleaned):
        return None, int(m.group(1))

    # 5) `EP04` / `E04`
    m = re.search(r"\bEP?\.?\s*(\d{1,3})\b", cleaned, re.IGNORECASE)
    if m:
        return None, int(m.group(1))

    # 6) 兜底：末尾孤立数字
    m = re.search(r"(?:^|\s)(\d{1,3})(?:v\d)?\s*$", cleaned.strip())
    if m:
        return None, int(m.group(1))

    return None, None


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_DECLARED_SEASON_RE = [
    # `2nd Season` / `3rd Season` / `4th SEASON`
    re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\s+season\b", re.I),
    # `Season 3` / `SEASON3`
    re.compile(r"\bseason\s*0*(\d{1,2})\b", re.I),
    # `S3 - 08` / `S01E58`（已归一化的名字也会命中，这正是我们要的：
    # 它声明的季号与库内季号一致，不会被判成冲突）
    re.compile(r"(?:^|[\s\[\]_.-])s0*(\d{1,2})(?=e\d|[\s\[\]_.-]|$)", re.I),
    # `第三季` / `第 3 期`
    re.compile(r"第\s*([0-9一二三四五六七八九十]{1,2})\s*[季期]"),
]


_SEASON_DIR_RE = re.compile(r"\s*season\s*(\d{1,3})\s*", re.IGNORECASE)


def season_of_dir(name: str) -> int | None:
    """季目录名 → 季号。`"Season 3"` → 3，`"Season 0"` → 0，不是季目录返回 None。

    **这是季目录解析的唯一实现，不要在别处再写一遍。** 2026-09-07 审计发现
    同一件事在 `scan` / `purge` / `grab` / `subscription` / `builtin` 里写了五遍、
    四种行为：有的锚定行尾、有的不锚，有的 `re.I`、有的不；于是 `Season  2`
    （双空格）和 `season 3`（小写）在扫描侧认不出、在抓取侧认得出，
    同一个目录在两条链路上是两个答案。

    这里取五者的并集并收紧尾部：大小写不敏感、容忍多余空白、但必须整名匹配
    （`Seasonal 4` 不算季目录）。
    """
    m = _SEASON_DIR_RE.fullmatch(name or "")
    return int(m.group(1)) if m else None


def declared_season(raw: str) -> int | None:
    """发布名里**明写**的季号；没写则返回 None。

    与 `parse_episode` 返回的 season 不同：那个是"能推出来的季号"，
    推不出来就退回目录/AutoBangumi 记录；这个只认发布方自己写的字，
    专门用来发现**发布方的季号与库内季号不一致**。

    出处（2026-08-31）：Re:Zero 在 TMDB 上是压平的单季连续编号（S1+S2+S3
    共 79 集），库内目录只有 `Season 1`。`[Fyy Raws] ... 3rd Season - 08`
    这个发布的集号 `08` 是**该季内**的第 8 集，实为连续编号第 58 集。
    改名规则拿 `08` 直接当季内集号，把它写成了 `S01E08`——正好撞上 2016 年
    真正的第 8 集；随后 duplicate-episode 规则把 1.31GB 的原片当重复清进了
    隔离区，只留下 487MB 的新文件。两条规则各自都"正确"，错在没人发现
    发布方说的是第三季而库里算的是第一季。
    """
    base = raw.rsplit("/", 1)[-1]
    for rx in _DECLARED_SEASON_RE:
        m = rx.search(base)
        if not m:
            continue
        tok = m.group(1)
        n = _CN_NUM.get(tok) if tok in _CN_NUM else int(tok)
        if n and 1 <= n <= 20:
            return n
    return None


@dataclass(frozen=True)
class Quality:
    """画质/字幕评分，用于重复集取舍。分数越高越优先保留。"""
    height: int          # 2160 / 1080 / 720 / 0=未知
    simplified: bool     # 含简体字幕
    traditional: bool
    is_bdrip: bool
    size: int            # 字节，同档次时作为码率代理

    def rank(self) -> tuple:
        # 顺序即优先级：分辨率 > 简体 > BDRip > 体积
        return (self.height, self.simplified, self.is_bdrip, self.size)


def parse_quality(filename: str, size: int = 0) -> Quality:
    """从文件名提取画质特征。规则：1080p 优先于 720p、简体优先于繁体。"""
    f = filename
    height = 0
    if re.search(r"\b(?:2160[pP]|4K|UHD)\b", f):
        height = 2160
    elif re.search(r"\b1080[pP]\b", f) or "1920x1080" in f:
        height = 1080
    elif re.search(r"\b720[pP]\b", f) or "1280x720" in f:
        height = 720

    simplified = bool(re.search(
        r"简|GB\b|CHS|SC\b|JPSC|scjp|\bsc\.|简日|简繁|Chs", f, re.IGNORECASE))
    traditional = bool(re.search(
        r"繁|BIG5|CHT|TC\b|JPTC|tcjp|\btc\.|繁日|Cht", f, re.IGNORECASE))
    is_bdrip = bool(re.search(r"BDRip|Blu-?Ray|BDBOX", f, re.IGNORECASE))
    return Quality(height, simplified, traditional, is_bdrip, size)


def target_filename(official_title: str, season: int, episode: int, ext: str) -> str:
    """生成规范文件名：`{official_title} S{NN}E{EE}{.ext}`。"""
    if not ext.startswith("."):
        ext = "." + ext
    return f"{official_title} S{season:02d}E{episode:02d}{ext}"


def target_subtitle_filename(
    official_title: str, season: int, episode: int, lang: str, ext: str
) -> str:
    """字幕文件保留语言后缀：`{title} S01E01.sc.ass`。"""
    if not ext.startswith("."):
        ext = "." + ext
    lang = lang.strip(".")
    return f"{official_title} S{season:02d}E{episode:02d}.{lang}{ext}"


def subtitle_lang_tag(filename: str) -> str | None:
    """提取字幕语言标记（scjp / tcjp / sc / tc / zh-CN ...）。"""
    m = re.search(r"\.([a-zA-Z]{2}(?:jp)?|zh-[A-Za-z]{2,4})\.[a-zA-Z]{3}$", filename)
    return m.group(1) if m else None


def is_normalized(filename: str, official_title: str) -> bool:
    """磁盘文件名是否已符合规范。

    注意：只能用磁盘文件名判断，不能用 qBittorrent 的 `name` 字段——
    renameFile 只改 content_path，不改 name，用 name 判断会大量误报。
    """
    stem = filename.rsplit(".", 1)[0]
    prefix = normalize(official_title) + " s"
    return normalize(stem).startswith(prefix)
