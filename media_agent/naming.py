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
    """是否为特典/菜单/PV 等非正片内容。**只看整个名字**——判断库里的文件请用 `is_extra_of`，
    它先去掉作品标题。"""
    return bool(_EXTRA_RE.search(filename))


def is_extra_of(filename: str, titles) -> bool:
    """去掉作品标题之后，名字里还有没有特典记号。

    规范名是 `{TMDB 标题} SxxEyy.ext`：标题里带 `trailer` / `preview` / `menu` / `PV` / `特典` /
    `菜单` / `creditless` 的番，整名匹配会把**每一集**都当成特典移进隔离区——与"The Ghost
    **in** the Shell 整部被判成特典"同一类（那次只修了 `IN` 一个记号）。所以先把 `titles`
    （TMDB 标题、目录名、AutoBangumi 的 official_title / title_raw）从名字里拿掉，只按词边界拿
    （标题 `K` 不能把 `mkv` 里的 k 也拿掉），再看剩下的部分。认不出的标题（发布名里的罗马音）
    拿不掉——由调用方的"某集唯一的文件不当特典"兜底。
    """
    s = normalize(filename)
    for t in sorted({normalize(t) for t in titles if t and t.strip()}, key=len, reverse=True):
        s = re.sub(r"(?<!\w)" + re.escape(t) + r"(?!\w)", " ", s)
    return bool(_EXTRA_RE.search(s))


_SXXEYY_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")


def explicit_slot(raw: str) -> tuple[int, int] | None:
    """名字里显式写着的 `SxxEyy`（改过名的规范名）→ (季, 集)；没有返回 None。"""
    m = _SXXEYY_RE.search(raw.rsplit("/", 1)[-1])
    return (int(m.group(1)), int(m.group(2))) if m else None


def offset_episode(n: int, offset: int) -> int | None:
    """原始集号加上这一季的集号偏移（sidecar 的 `episode_offsets`，迁移之前是 AutoBangumi 的 `episode_offset`）；
    换算出非正数返回 None。**文件名、番组页标题、抓取挑候选都用这一处**，三边对"这是第几集"不能各说各的。

    换算出非正数说明原始集号本来就是季内编号（`第三季 - 01` 配上 `-24`）。AutoBangumi 这时悄悄退回原始集号；
    这里不猜：一季里两种编号混着来的发布说不清是哪一集，认不出（不收、不改名、不参与判重），交给人——
    抓取把它报成"换算不进这一季"（`plugins/grab.py`）。"""
    m = int(n) + int(offset or 0)
    return m if m > 0 else None


def apply_episode_offset(raw: str, ep: int, offset: int) -> int | None:
    """集号偏移（这一季一个值）换算成季内集号。

    **只换算发布名里的原始集号，不碰已经是 `SxxEyy` 的名字**——那是换算过之后的结果。
    生产 AB 订阅 id 37（《超超超超超喜欢你的100个女朋友》第三季，`-24`）：`- 25` 换算成
    第 1 集，AB 据此改名成 `… S03E01.mkv`；以前判重再对 `S03E01` 减一次 24，落进
    `(3, -23)`，与刚下完的 `- 25`（`(3, 1)`）永远不在同一个桶里。

    换算出来不是正数（原始集号本来就是季内编号）返回 None——认不出，交给人，
    不要提议改成 `S03E-23`（`offset_episode`）。偏移为 0 时原样返回。
    """
    if not offset or _SXXEYY_RE.search(raw.rsplit("/", 1)[-1]):
        return ep
    return offset_episode(ep, offset)


def parse_episode(raw: str) -> tuple[int | None, int | None]:
    """从原始文件名解析 (season, episode)。season 为 None 表示未标注。

    覆盖实测过的所有命名风格；返回的 episode 可能是绝对集号，
    需要再用 bangumi.episode_offset 换算成季内集号。
    """
    base = raw.rsplit("/", 1)[-1]

    # 1) 显式 SxxExx —— 最可靠
    m = _SXXEYY_RE.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))

    cleaned = strip_noise(base)

    # 1.5) `第08集` / `第08话` / `第08話` —— 中文命名的字幕组常用。
    #      必须排在合集包判定之前：`第01-12集` 不是某一集。
    if re.search(r"第\s*\d{1,3}[-–~]\d{1,3}\s*[集话話]", cleaned):
        return None, None
    m = re.search(r"第\s*(\d{1,3})\s*[集话話]", cleaned)
    if m:
        return None, int(m.group(1))

    # 1.6) 合集包 `[01-12]` / `01-24TV全集` 不是"某一集"，明确返回未知。
    #      交给调用方按合集语义处理（见 subscription._max_episode）。
    #
    #      判据要窄：破折号两侧**不许有空格**、且都是 2-3 位数。
    #      放宽一点就会咬到 `Isekai Quartet 3 - 08`——那个 `3` 是片名里的
    #      季号，不是区间起点，实测被误判成合集包后整部剧的集号全丢。
    if re.search(r"[\[【\s](\d{2,3})[-–~](\d{2,3})", cleaned):
        return None, None

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


_PIN_RE = re.compile(r"\bma:S(\d{1,2})E(\d{1,3})\b")


def parse_pin(tags: str) -> tuple[int, int] | None:
    """种子标签里的 `ma:SxxExx` 集号钉子 → (季, 集)。

    钉子是抓取时打上去的，用来在"下完了但改名规则还没跑"那段窗口里
    仍然认得出这一集。**唯一实现**，不要在别处再写一遍这个正则。
    """
    m = _PIN_RE.search(tags or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


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


_AUTO = object()


def release_slot(raw: str, *, dir_season: int | None = None, ab_season: int | None = None,
                 offsets: dict | None = None, episode_offset=0,
                 declared=_AUTO, parsed: tuple | None = None) -> tuple[int, int] | None:
    """一个名字（文件名、种子显示名、番组页标题）→ 库内集位；认不出、换算不了返回 None。

    判重分桶（`builtin._resolve`）、发布名独立确认（`builtin._release_slot`）共用这一处，不能各算各的
    （出处账本里的**番组页标题**不走这里，走 `title_slot`：标题里的 `Sxx` 是发布方的编号，不定库内的季）：
    - 季：名字里写的 `Sxx` > 季目录 `Season N`（`dir_season`）> AutoBangumi 订阅的季（`ab_season`）> 1；
    - 发布方**声明**的季号（`declared`，默认按 `declared_season(raw)`）与库内季号不同：只有 sidecar 的
      `season_offsets` 有它才换算（`集号 <= 偏移` 才加上偏移——Fyy Raws 的 `3rd Season - 08` 是第 58 集，
      Dynamis One 的 `4th Season - 79` 就是第 79 集），没有就返回 None（2026-08-31：把 `3rd Season - 08` 写成
      `S01E08`，撞掉了 2016 年真正的第 8 集）；
    - 集号偏移只换算原始集号（`apply_episode_offset`），换算出非正数返回 None。`episode_offset` 是一个整数，
      或者 `库内季号 → 偏移` 的函数（`builtin.episode_offset_for`：sidecar 的 `episode_offsets` 按季登记，
      AutoBangumi 的只对它订阅的那一季）——按上面算出的季取。

    `parsed`：调用方已经 `parse_episode(raw)` 过就传进来，免得再解析一遍。"""
    season, ep = parsed if parsed is not None else parse_episode(raw)
    if ep is None:
        return None
    if season is not None:
        target = season
    elif dir_season is not None:
        target = dir_season
    else:
        target = int(ab_season) if ab_season else 1
    dec = declared_season(raw) if declared is _AUTO else declared
    if dec is not None and dec != target:
        off = (offsets or {}).get(str(dec))
        if off is None:
            return None
        if int(ep) <= int(off):
            ep = int(ep) + int(off)
    shift = episode_offset(target) if callable(episode_offset) else episode_offset
    ep = apply_episode_offset(raw, ep, int(shift or 0))
    if ep is None:
        return None
    return target, ep


def declared_seasons(title: str) -> set[int]:
    """番组页发布标题里明写的季号。按 ` / ` 分开的每一段各认一次：中文名、日文名、英文名常各写各的
    （`辉夜大小姐想让我告白 第三季 / Kaguya-sama wa Kokurasetai S3 - 03`），`declared_season` 只看最后一段。
    抓取挑候选（`slot_in_season`）与出处账本（`ledger.release_facts`、`title_slot`）共用这一处。"""
    out = set()
    for part in re.split(r"\s+/\s+", title or ""):
        ds = declared_season(part)
        if ds:
            out.add(ds)
    return out


# 特典位（第 0 季）只收标着特典的发布。只写集号的（`- 03`）是正片编号——辉夜那一页上别的组写
# `Kaguya-sama wa Kokurasetai - Ultra Romantic - 03`，`Ultra Romantic` 就是第三季的副标题，不声明季号。
SPECIAL_RE = re.compile(r"特别篇|特別篇|番外|总集篇|総集編|\bOVA\b|\bOAD\b|\bSP\s*\d|\bSpecials?\b",
                        re.IGNORECASE)


def slot_in_season(title: str, n: int, target: int,
                   offsets: dict[int, int]) -> tuple[int | None, str]:
    """集号为 `n` 的这个发布（番组页标题），落在库内第 `target` 季的第几集；不属于这一季返回 `(None, 为什么)`。

    - 没声明季号、或声明的就是目标季：第 `n` 集；
    - 声明了别的季、sidecar 的 `season_offsets` 有它：按偏移换算（`n <= 偏移` 才加，与改名 `_slot_from`
      同一口径：Fyy Raws 的 `3rd Season - 08` 是第 58 集，Dynamis One 的 `4th Season - 79` 就是第 79 集）；
      偏移只换算进正片季；
    - 声明了别的季、没有换算：不是这一季的（LAT-03）。桜都把入间同学的第四季标成「第3季」这类错位，
      在 sidecar 里登记 `season_offsets: {"3": 0}` 就收进来；
    - 目标是第 0 季（特典位）：只收标着特别篇 / OVA / SP（或 `S00Exx`）的——带季号的特典（`第三季 OVA`）
      也算；没有这些字样的是正片编号，不论声不声明季号。

    **抓取挑候选（`grab._slot_in_season`）与出处账本读集位（`title_slot`）共用这一处**：同一个标题两边以前各算各的，
    Re:Zero 的 `第四季 / … S04E15`（偏移 66）抓取算第 81 集、账本算 (4, 15)（2026-09-27 审查）。
    """
    declared = declared_seasons(title)
    if target == 0:
        if parse_episode(title)[0] == 0 or SPECIAL_RE.search(title):
            return n, ""
        if declared:
            return None, f"标的是第 {'/'.join(map(str, sorted(declared)))} 季的正片"
        return None, "正片编号（没有特别篇 / OVA / SP 字样）"
    if not declared or target in declared:
        return n, ""
    for ds in sorted(declared):
        off = offsets.get(ds)
        if off is not None:
            return (n + off if n <= off else n), ""
    return None, f"标的是第 {'/'.join(map(str, sorted(declared)))} 季"


def title_slot(title: str, *, target: int, offsets: dict | None = None,
               episode_offset: int = 0) -> tuple[tuple[int, int] | None, str]:
    """番组页发布标题 → 放在库内第 `target` 季（文件所在的季目录）时的集位；换算不了返回 `(None, 为什么)`。

    出处账本读集位（`builtin.ledger_view`）与补录（`ledger_backfill`）用它。与文件名那一套（`release_slot`）的区别：
    - **季由文件所在的库内季定，不由标题里的 `Sxx` 定。** 标题是发布方的编号：CR 系的 `第三季 / … S01E25` 里 `S01`
      是 TMDB 的连续编号；以前让它定季，《超超超超超喜欢你的100个女朋友》`Season 3/… S03E01.mkv`（AB 订阅第三季、
      `episode_offset -24` 改好的）被算成 (1, 25)、提议改成 `S01E25`（2026-09-27 生产快照，10 个文件）。账本永远不把
      文件挪到别的季。
    - **季内的换算与抓取同一套**（`slot_in_season`）：偏移只进正片季，特典位只认标着特典的。
    - **声明了不止一个季号**（`第三季 / … S01E25`）说不清按哪一季编号：返回 None，不取其中一个。
    - 集号偏移（`episode_offset`）由调用方按目标季取（`builtin.episode_offset_for`：sidecar 的 `episode_offsets`，
      没登记时是 AB 订阅在它那一季上的）；给了就对标题里的原始集号换算——标题从来不是改出来的名字，`S01E25` 也照换，
      AB 自己就是这么做的；换算出非正数认不出（`offset_episode`）。特典位不换算。
    """
    ep = parse_episode(title)[1]
    if ep is None:
        return None, "认不出集号"
    declared = declared_seasons(title)
    if len(declared) > 1:
        return None, f"声明了不止一个季号（{'/'.join(map(str, sorted(declared)))}），说不清按哪一季编号"
    offs = {int(k): int(v) for k, v in (offsets or {}).items() if str(k).strip().isdigit()}
    n, why = slot_in_season(title, int(ep), int(target), offs)
    if n is None:
        return None, why
    if episode_offset and target != 0:
        shifted = offset_episode(n, episode_offset)
        if shifted is None:
            return None, f"集号偏移 {int(episode_offset):+d} 换算出非正数（{n} → {int(n) + int(episode_offset)}）"
        n = shifted
    return (int(target), int(n)), ""


_SIMPLIFIED_RE = re.compile(
    r"简|GB\b|CHS|SC\b|JPSC|scjp|\bsc\.|简日|简繁|Chs|hans|simplified", re.IGNORECASE)
_TRADITIONAL_RE = re.compile(
    r"繁|BIG5|CHT|TC\b|JPTC|tcjp|\btc\.|繁日|Cht|hant|traditional", re.IGNORECASE)
_CHINESE_RE = re.compile(r"\bchi\b|\bzho\b|中文|中字", re.IGNORECASE)
# 蓝光源：文件名的画质判断（`parse_quality`）与出处账本记版本词（`ledger.versions_of`）共用
BDRIP_RE = re.compile(r"BDRip|Blu-?Ray|BDBOX", re.IGNORECASE)


def looks_simplified(s: str) -> bool:
    """这段文字是否指示简体中文。文件名、发布标题、字幕轨的 title 都用它。

    **唯一实现。** 2026-09-08 在 probe.py 里另写了一版 `\bsc\b`，匹配不到
    字幕轨 title 上的 `JPSC`（`JP` 和 `SC` 之间没有词边界），于是带简繁双轨的
    穹庐下的魔女 S01E11 被判成"只是普通中文"，与靠名字猜出的硬字幕同分，
    打平后按体积输给了零字幕轨的 h264 版本——而这一版正则里本来就写着 `JPSC`。

    2026-09-15 又漏一次：Netflix/爱奇艺这类多语言片源的字幕轨 title 写的是
    **英文全词** `Simplified Chinese` / `Traditional Chinese`，而这里原先只收
    中文字与 `CHS`/`SC` 这类缩写，于是《抓娃娃》13 条字幕轨里明明有简繁中文，
    却只被当成泛中文。教训同上：这个函数要吃的是"任何来源的语言标记"，
    文件名、发布标题、字幕轨 title 三种写法都要覆盖。
    """
    return bool(_SIMPLIFIED_RE.search(s or ""))


def looks_traditional(s: str) -> bool:
    return bool(_TRADITIONAL_RE.search(s or ""))


def looks_chinese(s: str) -> bool:
    """泛中文标记（`chi` / `zho` / 中文 / 中字），分不出简繁时用。"""
    return (looks_simplified(s) or looks_traditional(s)
            or bool(_CHINESE_RE.search(s or "")))


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

    simplified = looks_simplified(f)
    traditional = looks_traditional(f)
    is_bdrip = bool(BDRIP_RE.search(f))
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
