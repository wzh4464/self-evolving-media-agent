"""FakeLLM：默认关闭；测试不开口，它就不许被调用。

项目里有两条 LLM 路径：演进器提规则（evolution.py）和扫描时在多个 TMDB
候选里挑一个（`scan._pick_tmdb`）。后者决定番剧身份、进而决定改名目标——
它在测试里被**意外**调用，说明测试场景走进了一个它本不该碰的分支。

- `enabled` 默认 False：`_pick_tmdb` 走"不问模型"的确定性路径。
- `script(*answers)` / `when(pred, answer)` 打开它并排好答案。
- 未打开时被调用：记 tripwire `llm_unexpected`，返回 None（真实的降级路径）。
- 答案用完：返回 None，同样是真实降级路径。
"""
from __future__ import annotations

from typing import Callable


class FakeLLM:
    def __init__(self, tripwire=None):
        self.tripwire = tripwire
        self.enabled = False
        self.prompts: list[tuple[str, str]] = []
        self._queue: list[dict | None] = []
        self._rules: list[tuple[Callable[[str, str], bool], dict | None]] = []

    def script(self, *answers: dict | None) -> "FakeLLM":
        self.enabled = True
        self._queue.extend(answers)
        return self

    def when(self, predicate: Callable[[str, str], bool], answer: dict | None) -> "FakeLLM":
        """`predicate(system, user)` 为真时返回 `answer`。"""
        self.enabled = True
        self._rules.append((predicate, answer))
        return self

    def ask_json(self, system: str, user: str, retries: int = 2, on_error=None) -> dict | None:
        self.prompts.append((system, user))
        if not self.enabled:
            if self.tripwire is not None:
                self.tripwire.record("llm_unexpected", user[:200])
            return None
        for pred, ans in self._rules:
            if pred(system, user):
                return ans
        return self._queue.pop(0) if self._queue else None
