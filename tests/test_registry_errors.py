"""Registry 吞掉的检测器异常要记下来（规则 id、异常、出错位置），不能只打一行日志就没了。

单条规则崩溃不拖垮整轮是对的（kernel `run_all`）；但以前除了一行 `[registry] 规则 X 执行失败` 之外什么都
不留——critic N9：检测器里一个 "database is locked" 被吞掉，诊断就静默地少了一截（DuplicateEpisode 出错之后
的桶全丢）。健康报告要能说出"这一轮哪条规则崩了、崩在哪"。
"""
from __future__ import annotations

import pytest

from media_agent.kernel import Finding, Registry


class _Boom:
    id = "boom-rule"
    kind = "x"

    def detect(self, ctx, state):
        yield Finding(rule=self.id, kind="x", severity="minor", summary="先产出一条", show="s",
                      path="/a")
        self._helper()

    def _helper(self):
        raise RuntimeError("database is locked")


class _Fine:
    id = "fine-rule"
    kind = "y"

    def detect(self, ctx, state):
        yield Finding(rule=self.id, kind="y", severity="minor", summary="ok", show="s", path="/b")


@pytest.mark.allow("detector_error", match="boom-rule")
def test_crash_is_recorded_with_rule_error_and_where(lib):
    reg = Registry()
    reg.register(_Boom())
    reg.register(_Fine())

    found = reg.run_all(lib.context(), lib.scan())

    assert {f.rule for f in found} == {"boom-rule", "fine-rule"}   # 崩之前产出的与别的规则照常
    [err] = reg.errors
    assert err["rule"] == "boom-rule"
    assert err["error"] == "RuntimeError: database is locked"
    assert "test_registry_errors.py" in err["where"] and "_helper" in err["where"]
    # 日志行的格式不变（tripwire 与生产 run.err.log 的读者都认它）
    assert any("[registry] 规则 boom-rule 执行失败" in ln for ln in lib.logs)


@pytest.mark.allow("detector_error")
def test_each_run_all_starts_a_fresh_error_list(lib):
    reg = Registry()
    reg.register(_Boom())
    ctx, state = lib.context(), lib.scan()
    reg.run_all(ctx, state)
    reg.run_all(ctx, state)
    assert len(reg.errors) == 1


def test_no_errors_when_nothing_crashes(lib):
    reg = Registry()
    reg.register(_Fine())
    reg.run_all(lib.context(), lib.scan())
    assert reg.errors == []
