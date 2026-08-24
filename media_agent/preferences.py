"""择源偏好：从发布标题判断一个候选能不能要、有多想要。

**为什么需要它**：改成"谁先出要谁"之后，同一集可能同时有好几家的版本，
必须有个确定性的取舍规则，否则"最先出的"经常不是"能看的"。
实测教训：尼古喵喵 E08 当天只有 ABEMA 转载版，抓下来 711MB，
ffprobe 一查**零字幕轨、纯日语音轨**——先到手了，但看不了。

所以判定分两层：
  1. **硬门槛**（require）：不满足直接淘汰，多快都没用
  2. **偏好打分**（prefer / avoid）：在通过门槛的候选里排序

规则存在磁盘 `.agents/preferences.json`，每次运行重新读，改了立刻生效、
不用改代码。缺文件时用下面的 DEFAULT。

**局限要说清楚**：这是对发布标题的启发式判断，不是对文件内容的检验。
标题没写"简繁内封"但实际有中文字幕的，会被误杀；反过来标题写了却没有的，
会漏网。真正的验证只能等下载完 ffprobe——那时已经花了带宽。
在"先到先得"的目标下，这个取舍是划算的。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREFS_PATH = PROJECT_ROOT / ".agents" / "preferences.json"

DEFAULT: dict = {
    # 硬门槛：任一 any 命中才算通过；全部 require 条目都要过
    "require": [
        {
            "name": "中文字幕",
            "any": ["简", "繁", "中文", "CHS", "CHT", "GB", "BIG5",
                    "内封", "内嵌", "双语", "SC", "TC", "Chs", "Cht"],
            "why": "没有中文字幕的版本拿到也看不了。ABEMA/NF 等平台直转常是纯日语。",
        },
    ],
    # 偏好：命中加分
    "prefer": [
        {"name": "无删减", "weight": 100,
         "any": ["无修", "无删减", "无码", "未删减", "无修正", "uncensored",
                 "青蓝岛", "邪竜解放版"]},
        {"name": "简中", "weight": 50,
         "any": ["简", "CHS", "GB", "简体", "简日", "简繁", "SC"]},
        {"name": "内封软字幕", "weight": 20,
         "any": ["内封"]},
    ],
    # 规避：命中扣分
    "avoid": [
        {"name": "删减版", "weight": 90,
         "any": ["全遮", "修正版", "配信限定", "全面限制", "和谐"]},
        {"name": "仅繁中", "weight": 30,
         "any": ["繁体", "CHT", "BIG5", "繁日双语"]},
        {"name": "内嵌硬字幕", "weight": 10,
         "any": ["内嵌"]},
    ],
}


@dataclass
class Verdict:
    """一个候选的评估结果。"""
    acceptable: bool
    score: int = 0
    passed: list[str] = field(default_factory=list)   # 命中的加分项
    penalties: list[str] = field(default_factory=list)
    blocked_by: str = ""                              # 没过的硬门槛名

    def why(self) -> str:
        if not self.acceptable:
            return f"未通过硬门槛「{self.blocked_by}」"
        bits = []
        if self.passed:
            bits.append("+" + "/".join(self.passed))
        if self.penalties:
            bits.append("-" + "/".join(self.penalties))
        return f"{self.score:+d} " + " ".join(bits) if bits else f"{self.score:+d}"


def load_rules(path: Path | None = None) -> dict:
    """读磁盘上的偏好规则；没有就用默认。用户改了 JSON 立刻生效。"""
    p = path or PREFS_PATH
    if not p.exists():
        return DEFAULT
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT
    # 允许只覆盖其中一部分
    return {k: data.get(k, DEFAULT[k]) for k in ("require", "prefer", "avoid")}


def _hits(title: str, words: list[str]) -> bool:
    return any(w in title for w in words)


def evaluate(title: str, rules: dict | None = None) -> Verdict:
    """按规则评估一个发布标题。"""
    r = rules or load_rules()

    for req in r.get("require", []):
        if not _hits(title, req.get("any", [])):
            return Verdict(acceptable=False, blocked_by=req.get("name", "?"))

    v = Verdict(acceptable=True)
    for pref in r.get("prefer", []):
        if _hits(title, pref.get("any", [])):
            v.score += int(pref.get("weight", 0))
            v.passed.append(pref.get("name", "?"))
    for av in r.get("avoid", []):
        if _hits(title, av.get("any", [])):
            v.score -= int(av.get("weight", 0))
            v.penalties.append(av.get("name", "?"))
    return v


def pick_best(candidates: list[dict], rules: dict | None = None) -> tuple[dict | None, list[tuple[dict, Verdict]]]:
    """从候选里挑一个。candidates 每项需有 `title` 键。

    返回 (选中项 | None, [(候选, 评估) …] 全部评估结果，供解释)。

    排序：先按分数降序；同分时**取列表中靠前的**——调用方按发布时间倒序传入，
    于是同分取更新的那个。这落实"谁先出要谁"：在能看的候选里不挑肥拣瘦，
    但也不会为了快而收一个看不了的。
    """
    r = rules or load_rules()
    scored = [(c, evaluate(c.get("title", ""), r)) for c in candidates]
    ok = [(i, c, v) for i, (c, v) in enumerate(scored) if v.acceptable]
    if not ok:
        return None, scored
    ok.sort(key=lambda x: (-x[2].score, x[0]))
    return ok[0][1], scored
