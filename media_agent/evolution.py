"""自演进：发现规则覆盖不到的问题 → 提议新规则 → 影子验证 → 提升为正式插件。

闭环设计（形态借鉴 deepseek-harness 的 Agent Notes 生命周期）：

    残留检测 → LLM 提议 → 影子验证 → 提升/驳回
    residue     propose     validate    promote/reject
                   ↓            ↓            ↓
            proposed/     (验证报告)    implemented/ 或 rejected/

**演进产物是声明式 DSL（JSON），不是可执行代码。** 模型只能在既定的
字段/操作符词表内组合条件，无法要求内核执行任意逻辑——这是自演进能开全自动的前提。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .kernel import (
    Context, Finding, LibraryState, MediaFile, Registry, RuleSpec, Show,
    _FIELD_GETTERS, _OPS, load_rule_specs,
)
from .naming import VIDEO_EXTS, is_normalized, normalize

NOTES_ROOT = Path(__file__).resolve().parent.parent / ".agents" / "notes"
RULES_DIR = Path(__file__).resolve().parent.parent / ".agents" / "rules"
CLASSES = {"feature", "bug-fix", "simplification", "architecture", "process", "testing"}


# ---------------------------------------------------------------------------
@dataclass
class Residue:
    """一簇"存在异常但没有任何规则解释"的样本。"""
    signature: str                       # 聚类特征
    samples: list[dict] = field(default_factory=list)
    count: int = 0


def find_residue(state: LibraryState, findings: list[Finding]) -> list[Residue]:
    """找出规则解决不了的异常文件 —— 即"盲区"。

    盲区 = 以下两种之一：
    - 没有任何 Finding 指向它（完全没被看见）
    - 有 Finding 但**给不出动作**（看见了但不知道怎么办，典型是 `unparsable`）

    第二种才是主要来源：检测器能说"这文件名不规范"，却解析不出集号，
    于是既不能改名也不能删除，只能干瞪眼——正该由演进器为它立新规则。
    """
    explained = {f.path for f in findings if f.path and f.action is not None}
    clusters: dict[str, Residue] = {}

    for show in state.shows:
        title = show.official_title
        for f in show.files:
            if str(f.path) in explained:
                continue
            if f.is_incomplete or f.ext not in VIDEO_EXTS:
                continue
            if is_normalized(f.filename, title):
                continue

            sig = _signature(f, show)
            r = clusters.setdefault(sig, Residue(signature=sig))
            r.count += 1
            if len(r.samples) < 6:       # 每簇留几个样本给模型看
                r.samples.append({
                    "filename": f.filename,
                    "show_dir": f.show_dir,
                    "season_dir": f.season_dir,
                    "parent_dir": f.parent_dir,
                    "official_title": title,
                    "torrent_name": f.torrent_name,
                    "torrent_tags": f.torrent_tags,
                    "torrent_state": f.torrent_state,
                    "size": f.size,
                })

    return sorted(clusters.values(), key=lambda r: -r.count)


def _signature(f: MediaFile, show: Show) -> str:
    """把文件名抽象成结构特征，让同类异常聚到一起。"""
    name = f.filename
    name = re.sub(r"\d+", "#", name)
    name = re.sub(r"\s+", " ", name)
    bracket_style = "".join(sorted(set(re.findall(r"[\[\]【】()（）]", name))))
    return f"{f.ext}|{bracket_style}|{name[:60]}"


def find_failure_patterns(audit_log: Path, min_count: int = 3) -> list[dict]:
    """从审计日志里找反复失败的动作 —— 说明某条规则的判断有问题。"""
    if not audit_log.exists():
        return []
    counter: Counter = Counter()
    detail: dict[tuple, dict] = {}
    for line in audit_log.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") != "failed":
            continue
        key = (rec.get("rule"), rec.get("op"), (rec.get("error") or "")[:60])
        counter[key] += 1
        detail[key] = rec
    return [
        {"rule": k[0], "op": k[1], "error": k[2], "count": c, "sample": detail[k]}
        for k, c in counter.most_common() if c >= min_count
    ]


# ---------------------------------------------------------------------------
_PROPOSE_SYSTEM = """你是媒体库治理 agent 的规则演进器。

现有规则没能解释一批异常文件。你的任务：提议一条**声明式规则**来覆盖它们。

只能使用以下字段：
{fields}

只能使用以下操作符：
{ops}

可用动作及其**必需参数**（参数名必须一字不差，`path` 和 `torrent_hash` 由系统自动注入，你不要写）：
- `rename`   args: {{"new_name": "新文件名"}}
- `retag`    args: {{"tags": "标签字符串"}}
- `recategorize` args: {{"category": "分类名"}}
- `relocate` args: {{"location": "目标目录绝对路径"}}
- `write_nfo` args: {{"tmdb_id": 123, "title": "标题"}}
- `trash`    args: {{}}  （移入隔离区）

**`action` 字段是可选的。算不出确切的参数值时，直接省略整个 action 字段。**
不要为了"做个标记"而硬凑一个动作——写 `new_name: ""`、`tmdb_id: 0`、
`title: "待识别"` 这类占位值会被直接驳回。只报告问题的规则同样有价值，
它让这批文件从"没人看见"变成"有名字的已知问题"，这就够了。

返回 JSON：
{{
  "worth_a_rule": true/false,
  "reason": "为什么值得/不值得立规则",
  "rule": {{
    "id": "kebab-case-短标识",
    "kind": "问题类型_下划线",
    "severity": "critical|important|minor",
    "summary": "一句话说明这条规则抓什么",
    "match": {{"all": [{{"field": "...", "op": "...", "value": "..."}}]}},
    "action": {{"op": "...", "args": {{}}, "note": "..."}}
  }},
  "note": {{
    "class": "feature|bug-fix|simplification|architecture|process|testing",
    "title": "简短标题",
    "context": "观察到什么现象",
    "decision": "决定加这条规则做什么",
    "alternatives": "考虑过但放弃的方案",
    "risk": "误伤风险与缓解"
  }}
}}

严格要求：
- 规则必须**只**命中这批异常，绝不能命中已规范的文件。宁可窄，不可宽。
- 只有一两个样本、或明显是一次性个例时，返回 worth_a_rule=false。
- 涉及删除的动作要格外保守，不确定就不给 action。"""


# 每种动作必须提供的参数（`path` / `torrent_hash` 由 DSL 解释器自动注入）
_ACTION_CONTRACT: dict[str, set[str]] = {
    "rename": {"new_name"},
    "retag": {"tags"},
    "recategorize": {"category"},
    "delete_category": {"category"},
    "relocate": {"location"},
    "write_nfo": {"tmdb_id", "title"},
    "trash": set(),
}


def _validate_action_schema(spec: RuleSpec) -> str | None:
    """检查提议的动作是否符合执行器契约。返回错误说明，合规返回 None。"""
    if not spec.action:
        return None
    op = spec.action.get("op")
    if op not in _ACTION_CONTRACT:
        return f"未知动作 `{op}`，可用：{sorted(_ACTION_CONTRACT)}"
    required = _ACTION_CONTRACT[op]
    args = spec.action.get("args") or {}
    given = set(args.keys())
    missing = required - given
    if missing:
        return f"动作 `{op}` 缺少必需参数 {sorted(missing)}（给出的是 {sorted(given)}）"

    # 参数存在还不够，值必须真的可用。模型会写出 new_name="" 这种
    # "我知道该改名但算不出目标名"的半成品——执行它等于把文件改名成空串。
    for k in required:
        v = args.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return (f"动作 `{op}` 的参数 `{k}` 为空——"
                    f"算不出具体值就不要给 action，只报告问题即可")
        # 数值型参数（如 tmdb_id）必须是有效正数，0 是典型的"我不知道"信号
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v <= 0:
            return (f"动作 `{op}` 的参数 `{k}` 是 {v}，不是有效值——"
                    f"查不到真实值就不要给 action")
        if isinstance(v, str) and re.search(
            r"^(TODO|TBD|placeholder|unknown|n/?a|待定|待识别|待处理|待确认|未知|暂无|"
            r"xxx+|<.*>|\{.*\})$", v.strip(), re.I,
        ):
            return f"动作 `{op}` 的参数 `{k}` 是占位符 `{v}`，不是真实值"
    return None


class Evolver:
    def __init__(self, ctx: Context, registry: Registry):
        self.ctx = ctx
        self.registry = registry
        RULES_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- 提议 ----------------
    def propose(self, residue: Residue, state: LibraryState) -> dict | None:
        if not (self.ctx.llm and self.ctx.llm.enabled):
            return None
        existing = [
            {"id": getattr(d, "id", "?"), "kind": getattr(d, "kind", "?")}
            for d in self.registry.detectors
        ]
        system = _PROPOSE_SYSTEM.format(
            fields=", ".join(sorted(_FIELD_GETTERS)),
            ops=", ".join(sorted(_OPS)),
        )
        user = json.dumps({
            "未被解释的异常样本": residue.samples,
            "该簇总数": residue.count,
            "现有规则": existing,
            "说明": "这些文件的命名不符合 `{official_title} SxxExx.ext` 规范，但现有规则都没命中。",
        }, ensure_ascii=False, indent=2)

        ans = self.ctx.llm.ask_json(system, user)
        if not ans or not ans.get("worth_a_rule") or not ans.get("rule"):
            return None
        return ans

    # ---------------- 影子验证 ----------------
    def validate(self, spec: RuleSpec, state: LibraryState,
                 residue: Residue) -> tuple[bool, dict]:
        """在当前快照上空跑候选规则。

        通过条件（全部满足）：
        1. 至少命中残留簇里的一个样本 —— 证明它确实解决了问题
        2. **零误伤**：不命中任何已规范的文件
        3. 命中范围不超过残留簇规模的 3 倍 —— 防止写得过宽
        4. 与现有规则不重叠
        """
        # 动作契约先验：模型很容易把参数名写错（实测给出 `tag` 而执行器要 `tags`），
        # 这种错误在执行时才炸，必须在验证阶段就拦掉。
        schema_err = _validate_action_schema(spec)
        if schema_err:
            return False, {"verdict": "REJECT", "hits": 0,
                           "reject_reasons": [f"动作参数不合契约：{schema_err}"]}

        hits: list[str] = []
        false_positives: list[str] = []

        for finding in spec.detect(self.ctx, state):
            hits.append(finding.path)

        residue_names = {s["filename"] for s in residue.samples}
        by_path = {str(f.path): (f, s) for s in state.shows for f in s.files}

        for p in hits:
            entry = by_path.get(p)
            if not entry:
                continue
            f, show = entry
            if is_normalized(f.filename, show.official_title):
                false_positives.append(p)

        covered = any(Path(p).name in residue_names for p in hits)
        overlap = self._overlaps_existing(spec, state, hits)

        report = {
            "hits": len(hits),
            "covered_residue": covered,
            "false_positives": len(false_positives),
            "false_positive_samples": false_positives[:5],
            "residue_size": residue.count,
            "overlap_with": overlap,
        }
        ok = (
            covered
            and not false_positives
            and hits
            and len(hits) <= max(residue.count * 3, 5)
            and not overlap
        )
        report["verdict"] = "PASS" if ok else "REJECT"
        if not ok:
            reasons = []
            if not covered:
                reasons.append("没有覆盖它本该解决的样本")
            if false_positives:
                reasons.append(f"误伤 {len(false_positives)} 个已规范文件")
            if not hits:
                reasons.append("一个都没命中")
            if len(hits) > max(residue.count * 3, 5):
                reasons.append(f"命中 {len(hits)} 个，远超残留规模 {residue.count}，规则过宽")
            if overlap:
                reasons.append(f"与现有规则 {overlap} 重叠")
            report["reject_reasons"] = reasons
        return ok, report

    def _overlaps_existing(self, spec: RuleSpec, state: LibraryState,
                           hits: list[str]) -> str | None:
        """是否与现有规则重复。

        只有当现有规则**能对同一文件给出动作**时才算重叠。
        像 `unrenamed-file` 那样"检测到但解析不出集号、给不出动作"的情况，
        正是新规则要填补的空白，不构成重叠——否则演进器永远无法为
        任何已被标记 `unparsable` 的文件立规则。
        """
        for d in self.registry.detectors:
            if getattr(d, "id", "") == spec.id:
                return getattr(d, "id")
            try:
                actionable = {f.path for f in d.detect(self.ctx, state)
                              if f.path and f.action is not None}
            except Exception:
                continue
            if actionable and set(hits) & actionable:
                return getattr(d, "id", "?")
        return None

    # ---------------- 落盘 ----------------
    def write_note(self, lifecycle: str, note: dict, spec: RuleSpec,
                   validation: dict) -> Path:
        cls = note.get("class", "feature")
        if cls not in CLASSES:
            cls = "feature"
        slug = re.sub(r"[^a-z0-9]+", "-", spec.id.lower()).strip("-")
        d = NOTES_ROOT / lifecycle / cls
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{date.today().isoformat()}-{slug}.md"
        path.write_text(
            f"# {note.get('title', spec.id)}\n\n"
            f"Status: {lifecycle}\n"
            f"Rule: `{spec.id}`\n"
            f"Kind: `{spec.kind}`\n"
            f"Generated-by: media-agent evolver ({self.ctx.config.llm_model})\n\n"
            f"## 现象\n\n{note.get('context','')}\n\n"
            f"## 决定\n\n{note.get('decision','')}\n\n"
            f"## 放弃的替代方案\n\n{note.get('alternatives','')}\n\n"
            f"## 风险与缓解\n\n{note.get('risk','')}\n\n"
            f"## 影子验证\n\n```json\n"
            f"{json.dumps(validation, ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 规则定义\n\n```json\n"
            f"{json.dumps(spec.__dict__, ensure_ascii=False, indent=2)}\n```\n",
            encoding="utf-8",
        )
        return path

    def promote(self, spec: RuleSpec) -> Path:
        """验证通过 → 写入 .agents/rules/，下一轮自动挂载。"""
        path = RULES_DIR / f"{spec.id}.json"
        payload = dict(spec.__dict__)
        payload["source"] = "evolved"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ---------------- 一轮完整演进 ----------------
    def evolve(self, state: LibraryState, findings: list[Finding],
               max_proposals: int = 3) -> list[dict]:
        results: list[dict] = []
        residues = find_residue(state, findings)
        if not residues:
            return results

        for residue in residues[:max_proposals]:
            if residue.count < 2:
                continue        # 单个个例不值得立规则

            proposal = self.propose(residue, state)
            if not proposal:
                results.append({"signature": residue.signature, "count": residue.count,
                                "outcome": "no_proposal",
                                "detail": "模型认为不值得立规则或调用失败"})
                continue

            try:
                spec = RuleSpec.from_json(proposal["rule"])
            except Exception as e:
                results.append({"signature": residue.signature, "outcome": "malformed",
                                "detail": str(e)})
                continue

            ok, report = self.validate(spec, state, residue)
            note = proposal.get("note", {})

            if ok:
                note_path = self.write_note("implemented", note, spec, report)
                rule_path = self.promote(spec)
                self.registry.register(spec)     # 本轮即刻生效
                results.append({"signature": residue.signature, "count": residue.count,
                                "outcome": "promoted", "rule_id": spec.id,
                                "note": str(note_path), "rule_file": str(rule_path),
                                "validation": report})
            else:
                note_path = self.write_note("rejected", note, spec, report)
                results.append({"signature": residue.signature, "count": residue.count,
                                "outcome": "rejected", "rule_id": spec.id,
                                "note": str(note_path), "validation": report})
        return results


def load_evolved(registry: Registry) -> int:
    """把 .agents/rules/ 里已提升的规则挂载进注册表。"""
    specs = load_rule_specs(RULES_DIR)
    for s in specs:
        registry.register(s)
    return len(specs)
