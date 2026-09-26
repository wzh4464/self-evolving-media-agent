"""每部番目录下的 `.media-agent.json` —— 采集决策的随身档案。

**为什么需要它。** 这套系统里的"真相"散落在三处，且各自会漂移：

| 位置 | 存了什么 | 漂移方式 |
|---|---|---|
| AutoBangumi DB | title_raw / group_name / save_path | 目录改名后 save_path 需手动同步；bangumi_id 大量为空 |
| qBittorrent | 分类 / 标签 / 文件路径 | 分类会碎片化；`info.name` 与 `files.name` 语义不同 |
| 磁盘 | 目录名 / 文件名 | 被 TMDB 标题对齐整体改过 |

结果是"订阅 ↔ 目录"的反查要连试三种方式（save_path → official_title → 归一化模糊）
才勉强对上。更糟的是，**没有任何一处记录"这部番当前该用哪个字幕组、上次更新到几集"**——
字幕组弃坑只能靠人发现"怎么没有后续了"。

这个 sidecar 不是再镜像一份别处已有的数据，而是记录**别处根本没有的东西**：
media-agent 自己的采集决策、观察到的别名、源的更替历史。它跟着目录走，
目录改名/搬迁都不会丢，AutoBangumi 的库炸了也能据此重建订阅。

文件名以 `.` 开头，Jellyfin/Infuse 不会扫描它。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

SIDECAR_NAME = ".media-agent.json"
SCHEMA_VERSION = 1


@dataclass
class SourceRecord:
    """一次"用哪个源"的决策。"""
    group: str                      # 字幕组
    rss_link: str = ""
    adopted_on: str = ""            # 何时采用
    retired_on: str = ""            # 何时弃用（空 = 仍在用）
    last_episode: int = 0           # 该源出到第几集
    reason: str = ""                # 采用/弃用的原因


@dataclass
class Sidecar:
    schema_version: int = SCHEMA_VERSION
    updated_at: str = ""

    # --- 身份：各处叫什么 ---
    canonical_title: str = ""       # 规范名（= 目录名 = TMDB 本地化标题）
    tmdb_id: int | None = None      # 身份：扫描照它认，代码只在还没有时填一次，之后只有人改
    tmdb_source: str = ""           # 这个 tmdb_id 是怎么来的：search（扫描搜到的）、空 = 历史记录；
                                    # 人改 tmdb_id 时可以写 human
    tmdb_title: str = ""
    aliases: list[str] = field(default_factory=list)   # 实际发布标题里见过的名字

    # --- 订阅 ---
    bangumi_id: int | None = None   # AutoBangumi 记录 id（可能失效，故不作唯一依据）
    mikan_id: str = ""              # Mikan 番组页 id。先到先得的抓取盯的是整个番组页
                                    # （不带 subgroupid = 全字幕组）。抓取把它当候选之一、
                                    # 按播出日期打分；**代码不再写它**（以前检测期写回，
                                    # 2026-09-26 起选中的页记在 state/ 的缓存里），
                                    # 现存的值是历史记录或人填的。
    sources: list[dict] = field(default_factory=list)  # SourceRecord 列表，含历史

    # --- 编号 ---
    season_offsets: dict[str, int] = field(default_factory=dict)
    # {"3": 50} = 发布方写的"第 3 季"，其之前累计 50 集。
    # 用于 TMDB 把多季压平成单季连续编号、而发布组仍按分季编号的番。
    # 同一部番里两种习惯可能并存（Fyy Raws 写 `3rd Season - 08` 指第 58 集，
    # Dynamis One 写 `4th Season - 79` 就是第 79 集），所以偏移只在
    # 集号 <= 偏移量时才加——两种解释的取值区间不重叠。

    # --- 版本 ---
    require_any: list[str] = field(default_factory=list)
    # 这部番**只要**发布标题含其中任一词的版本（用户口径「只保留 X 版」）。
    # 叠加在全局 preferences 的硬门槛之上，**只在抓取选源时把关**——那时手里
    # 有 Mikan 的完整发布标题。下载之后就认不出来了：种子内部名常常不标版本，
    # LoliHouse 的邪竜解放版内部名就是 `[LoliHouse] Yani Neko - 11 [...]`，
    # 2026-09-18 我正是凭这个把库里十集邪竜解放版错认成了 TV 版。

    # --- 进度 ---
    seasons: dict[str, dict] = field(default_factory=dict)
    # {"1": {"have": [1,2,3], "aired": 7, "total": 12, "next_air": "2026-08-22"}}

    # --- 观察记录 ---
    notes: list[str] = field(default_factory=list)

    # --- 人钉住的字段 ---
    pinned: list[str] = field(default_factory=list)
    # 这里列出的字段由人说了算，代码不改它们：`mikan_id` 钉住 = 抓取只用这一页、不再按日期挑；
    # `tmdb_title` 钉住 = 改名 / 目录名 / 分类用这个标题，不跟 TMDB（也不受标题稳定闸约束）。

    @property
    def current_source(self) -> dict | None:
        for s in self.sources:
            if not s.get("retired_on"):
                return s
        return None

    def adopt_source(self, group: str, rss_link: str, reason: str = "") -> None:
        """换源：把当前源标记为退役，登记新源。保留历史，别覆盖。"""
        today = date.today().isoformat()
        cur = self.current_source
        if cur:
            if cur.get("group") == group and cur.get("rss_link") == rss_link:
                return                       # 没变化
            cur["retired_on"] = today
            if not cur.get("reason"):
                cur["reason"] = "被替换"
        self.sources.append(asdict(SourceRecord(
            group=group, rss_link=rss_link, adopted_on=today, reason=reason)))

    def add_alias(self, alias: str) -> bool:
        alias = (alias or "").strip()
        if alias and alias not in self.aliases:
            self.aliases.append(alias)
            return True
        return False

    def note(self, text: str) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        self.notes.append(f"[{stamp}] {text}")
        del self.notes[:-50]            # 只留最近 50 条


# ---------------------------------------------------------------- 字段归属
# 2026-09-26 起明文：写档案时各字段听谁的。`_op_write_sidecar` 以前拿诊断期的快照整份覆盖，诊断之后人改的
# `season_offsets`、另一个进程写的、回退写回去的都被盖掉；这里的四类是唯一口径（测试要求每个字段恰好归一类）。
#
# DERIVED：sidecar-sync / 抓取按盘上、AutoBangumi、TMDB 算出来的。写的时候用这一轮算的值（`aliases` 只增不减，
#          取并集；列在 `pinned` 里的除外）。算错了下一轮再算。
DERIVED = frozenset({"canonical_title", "tmdb_title", "aliases", "bangumi_id", "sources", "seasons"})
# IDENTITY：TMDB 身份。代码只在**还没有**时填一次（sidecar-sync 按扫描的结果），填上之后只有人改——
#           身份一变，改名目标、目录名、分类全跟着变（LAT-04：同一个目录的文件在两个标题之间来回改名）。
IDENTITY = frozenset({"tmdb_id", "tmdb_source"})
# USER_INTENT：人写的。代码从不写它们；写档案时一律以**此刻文件里的**为准，payload 里带的旧值不算数。
#              `mikan_id` 2026-09-26 起也归这里：抓取不再写它（选中的页记在 state/ 缓存）。
USER_INTENT = frozenset({"season_offsets", "require_any", "notes", "mikan_id", "pinned"})
BOOKKEEPING = frozenset({"schema_version", "updated_at"})


def asdict_of(sc: 'Sidecar') -> dict:
    return asdict(sc)


def path_for(show_dir: Path) -> Path:
    return show_dir / SIDECAR_NAME


class SidecarCorrupt(Exception):
    """sidecar 解析不了（不是 JSON、不是对象）。**不许覆盖**：里面也许有人写的 `season_offsets` /
    `require_any` / `notes`，读成默认值再写回去就全没了。写之前已备份（`backup`）。"""

    def __init__(self, path: Path, problem: str, backup: Path | None = None):
        self.path, self.problem, self.backup = path, problem, backup
        where = f"，已备份到 {backup.name}" if backup else ""
        super().__init__(f"{path.name} 解析不了（{problem}），不覆盖{where}；修好或删掉之后下一轮再写")


def _parse(text: str) -> tuple[dict | None, str]:
    """文件内容 → (对象, 问题)。不是合法 JSON、顶层不是对象都算坏。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e.msg}（第 {e.lineno} 行第 {e.colno} 列）"
    if not isinstance(data, dict):
        return None, f"顶层是 {type(data).__name__}，不是对象"
    return data, ""


def read_raw(show_dir: Path) -> tuple[dict | None, str]:
    """此刻文件里的原样内容（含本版本不认识的键）。不存在 → (None, "")；坏了 / 读不了 → (None, 问题)。"""
    p = path_for(show_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except (OSError, UnicodeDecodeError) as e:
        return None, f"读不了：{type(e).__name__}: {e}"
    return _parse(text)


def _from_dict(show_dir: Path, data: dict | None) -> 'Sidecar':
    if not data:
        return Sidecar(canonical_title=show_dir.name)
    known = set(Sidecar.__dataclass_fields__)
    return Sidecar(**{k: v for k, v in data.items() if k in known})


def load_checked(show_dir: Path) -> tuple['Sidecar', str]:
    """读档案，并说出它是不是坏的：`(sidecar, 问题)`，好的问题为空。坏的照样给一份默认值让读的一方继续，
    但**写的一方**必须看问题（`save` / `update` / `write_merged` 会拒绝覆盖坏文件）。"""
    data, problem = read_raw(show_dir)
    return _from_dict(show_dir, data), problem


def load(show_dir: Path) -> Sidecar:
    """只读的一方用：坏文件给默认值（检测照常跑，坏档案由 sidecar-sync 报出来）。"""
    return load_checked(show_dir)[0]


def backup_corrupt(show_dir: Path) -> Path | None:
    """把坏掉的档案原样拷一份到 `.media-agent.json.corrupt-<时间>`；已有内容相同的备份就不再拷。
    拷不出来返回 None（不抛：拒绝覆盖才是要紧的，备份是多一层保险）。"""
    p = path_for(show_dir)
    try:
        body = p.read_bytes()
    except OSError:                      # 连读都读不了：没有可备份的，调用方照样拒绝覆盖
        return None
    for old in sorted(show_dir.glob(SIDECAR_NAME + ".corrupt-*")):
        try:
            if old.read_bytes() == body:
                return old
        except OSError:                  # 读不了的旧备份不算"已有相同的"，接着比下一份
            continue
    dst = show_dir / f"{SIDECAR_NAME}.corrupt-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    try:
        shutil.copy2(p, dst)
    except OSError:                      # 拷不出来：返回 None，拒绝覆盖的报错里就不写备份位置
        return None
    return dst


def _current_for_write(show_dir: Path) -> dict | None:
    """写之前读此刻的文件；坏的就备份并抛 `SidecarCorrupt`。"""
    data, problem = read_raw(show_dir)
    if problem:
        raise SidecarCorrupt(path_for(show_dir), problem, backup_corrupt(show_dir))
    return data


def write_text_atomic(p: Path, text: str) -> None:
    """同目录临时文件 + 原子替换。临时名各不相同：以前固定叫 `.media-agent.json.tmp`，两个写的人会抢同一个。"""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:                  # 临时文件已经换上去了 / 本来就没建成：没什么可清的
            pass
        raise


def _write(show_dir: Path, data: dict) -> Path:
    data = dict(data)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    data["schema_version"] = SCHEMA_VERSION
    p = path_for(show_dir)
    write_text_atomic(p, json.dumps(data, ensure_ascii=False, indent=2))
    return p


def save(show_dir: Path, sc: Sidecar) -> Path:
    """整份写（此刻文件里本版本不认识的键保留）。此刻的文件是坏的就拒绝（`SidecarCorrupt`）。"""
    current = _current_for_write(show_dir) or {}
    sc.updated_at = datetime.now().isoformat(timespec="seconds")
    sc.schema_version = SCHEMA_VERSION
    return _write(show_dir, {**current, **asdict(sc)})


def update(show_dir: Path, mutate: Callable[[Sidecar], None]) -> Sidecar:
    """读-改-写：按此刻的文件改（`mutate` 改一份 Sidecar），不认识的键保留；坏文件拒绝（`SidecarCorrupt`）。"""
    current = _current_for_write(show_dir)
    sc = _from_dict(show_dir, current)
    mutate(sc)
    _write(show_dir, {**(current or {}), **asdict(sc)})
    return sc


def merge_for_write(current: dict | None, payload: dict) -> dict:
    """sidecar-sync 算的 payload 与**此刻文件**合并：只有派生字段听 payload 的（见上面的归属表）。

    - 文件不存在：payload 就是全部；
    - DERIVED：用 payload 的（payload 里没有的键不动）；`aliases` 取并集；列在 `pinned` 里的不动；
    - IDENTITY：文件里已有 tmdb_id 就不动（payload 若是另一个身份，它算的 tmdb_title 也不要）；没有才填；
    - USER_INTENT 与不认识的键：一律保留此刻文件里的。
    """
    if current is None:
        return dict(payload)
    out = dict(current)
    pinned = set(current.get("pinned") or [])
    same_identity = True
    if current.get("tmdb_id"):
        same_identity = payload.get("tmdb_id") in (None, current.get("tmdb_id"))
    elif payload.get("tmdb_id"):
        out["tmdb_id"] = payload["tmdb_id"]
        out["tmdb_source"] = payload.get("tmdb_source") or ""
    for k in DERIVED:
        if k not in payload or k in pinned:
            continue
        if k == "tmdb_title" and not same_identity:
            continue
        if k == "aliases":
            merged = list(current.get("aliases") or [])
            merged += [a for a in (payload.get("aliases") or []) if a not in merged]
            out[k] = merged
        else:
            out[k] = payload[k]
    return out


def write_merged(show_dir: Path, payload: dict,
                 adjust: Callable[[dict], None] | None = None) -> dict:
    """按此刻的文件合并 payload 后写（`merge_for_write`）；`adjust(合并结果)` 在写之前再改一次
    （执行器用它并回本轮抓的集）。坏文件拒绝（`SidecarCorrupt`）。返回写下去的内容。"""
    merged = merge_for_write(_current_for_write(show_dir), payload)
    if adjust is not None:
        adjust(merged)
    _write(show_dir, merged)
    return merged
