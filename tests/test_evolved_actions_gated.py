"""演进规则（LLM 提议、影子验证后自动上线的 DSL）带的动作，一律不自动执行。

critic N5：演进器的提示词允许 `trash` / `retag` / `recategorize` / `relocate` /
`write_nfo` 等动作，值由模型选。影子验证只在上线那一刻跑一次，此后再不复核；
`args.setdefault("path", …)` 还允许模型自带 `path` 覆盖。一条 `retag ma:SxxExx`
会成为 `_resolve`、封存、改名到钉子、`_inflight` 的权威依据——这是一条未经人审的
"LLM → 改名 / 判重删除"通路。

今天生产上 30 条演进规则的 `action` 全是 null，所以这道闸行为中立；它关掉的是
下一条带动作的规则上线那一刻。正式的"人工确认后放行"流程留到后面的阶段。
"""
from __future__ import annotations

import json

import pytest

from media_agent import cli
from media_agent import evolution as evolution_mod
from media_agent.kernel import RuleSpec


def _rule(action: dict, **kw) -> dict:
    return {"id": "llm-proposed", "kind": "llm_kind", "severity": "minor",
            "summary": "模型提议", "match": {"field": "filename", "op": "regex",
                                           "value": "S01E01"},
            "action": action, **kw}


@pytest.mark.parametrize("action", [
    {"op": "trash", "args": {}},
    {"op": "trash", "args": {"path": "{season}"}},          # 模型自带 path 覆盖
    {"op": "retag", "args": {"tags": "ma:S01E09"}},
    {"op": "recategorize", "args": {"category": "别的番"}},
    {"op": "rename", "args": {"new_name": "测试番 S01E09.mkv"}},
])
def test_evolved_rule_actions_are_never_executed(lib, action):
    s1 = lib.show("测试番").season(1)
    t = s1.single("测试番 S01E01.mkv", name="[G] Show - 01.mkv")
    action = json.loads(json.dumps(action).replace("{season}", str(s1.path)))
    rules = evolution_mod.RULES_DIR
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "llm-proposed.json").write_text(json.dumps(_rule(action), ensure_ascii=False),
                                             encoding="utf-8")
    before = lib.snapshot()

    c = lib.cycle(detectors=cli.build_registry().detectors)   # 生产的注册表：内置 + 演进

    [f] = [f for f in c.findings if f.rule == "llm-proposed"]
    assert f.action is not None                                # 规则确实带着动作……
    [skip] = [r for r in c.report.skipped if r["rule"] == "llm-proposed"]
    assert "演进规则未经人工确认" in skip["reason"]            # ……但执行器拒绝了它
    assert not [r for r in c.report.applied if r["rule"] == "llm-proposed"]
    assert lib.snapshot() == before
    assert lib.qbit.has(t.hash)


def test_rule_file_cannot_claim_to_be_builtin(lib):
    """`source` 字段来自 JSON 本身，模型写 `"source": "builtin"` 也不能绕过。"""
    s1 = lib.show("测试番").season(1)
    s1.local("测试番 S01E01.mkv", size=1000)
    spec = RuleSpec.from_json(_rule({"op": "trash", "args": {}}, source="builtin"))
    before = lib.snapshot()

    c = lib.cycle(detectors=[spec])

    assert not c.report.applied and len(c.report.skipped) == 1
    assert lib.snapshot() == before


def test_actionless_evolved_rules_are_unaffected(lib):
    """生产上 30 条规则全是无动作的归类规则：照常出 finding、照常算"已解释"。"""
    s1 = lib.show("测试番").season(1)
    s1.local("测试番 S01E01.mkv", size=1000)
    spec = RuleSpec.from_json(_rule(None))

    c = lib.cycle(detectors=[spec])

    [f] = c.findings
    assert f.action is None and f.classified
    assert not (c.report.applied or c.report.skipped or c.report.failed)
