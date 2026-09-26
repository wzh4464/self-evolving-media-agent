"""Tripwire：让"不完整的假对象"没法产出一个空洞的绿灯。

本项目到处都在吞异常，而且吞得有理由——单条规则崩溃不能拖垮整轮：
- `Registry.run_all` 把检测器异常记一行日志就继续（kernel.py `run_all`）；
- `Executor.apply` 把动作异常记成 `failed` 审计；
- `scan._torrent_files` 把 `files()` 的任何错误缓存成空列表；
- 各检测器里一串 `except Exception: continue`。

生产上这是韧性，测试里这是陷阱：假对象少实现一个方法、少配一条 URL，
被测代码照样"跑完"，断言看到的只是"什么都没发生"，于是测试绿了——
而它什么都没测。2026-09-26 的抓取记账事故就是这种形态：12 次抓取全记
`failed`，外面看一切正常。

所以每个测试结束时统一核对这里收集到的事件，未经声明的一律判失败。
测试可以用 `@pytest.mark.allow("kind", ...)` 或 `tripwire.allow(kind, match=...)`
声明"这是我预期的"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 事件种类。新增种类要同步写进 api 文档（tests/harness/__init__.py）。
KINDS = {
    "unrouted_url":    "urlopen 访问了没配路由的 URL（FakeWeb）",
    "network":         "有代码试图直连网络（socket 守卫）",
    "subprocess":      "有代码试图起子进程（docker/ffprobe 守卫）",
    "unmodeled":       "调用了假对象没建模的方法",
    "qbit_error":      "FakeQbit 按真实语义抛了错（404/409），且不是测试注入的",
    "qbit_overwrite":  "renameFile/setLocation 覆盖了磁盘上已存在的文件",
    "tmdb_unknown":    "FakeTMDB 被问到未登记的 id",
    "llm_unexpected":  "测试没开启 FakeLLM，代码却调用了它",
    "failed_record":   "Executor 写了一条 status=failed 的审计",
    "detector_error":  "Registry 吞掉了某个检测器的异常",
    "log_failure":     "ctx.log 打出了含「失败」的行（被吞掉的错误）",
}

_DETECTOR_ERR = re.compile(r"\[registry\] 规则 .* 执行失败")


@dataclass
class Event:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass
class Tripwire:
    events: list[Event] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    _allowed: list[tuple[str, re.Pattern | None]] = field(default_factory=list)

    # ---- 记录 ----
    def record(self, kind: str, detail: str) -> None:
        assert kind in KINDS, f"未知 tripwire 种类 {kind!r}"
        self.events.append(Event(kind, str(detail)))

    def on_log(self, msg: str) -> None:
        """所有 Context 的 `ctx.log` 都会被接到这里（见 conftest 的 Context 包装）。"""
        self.logs.append(msg)
        if _DETECTOR_ERR.search(msg):
            self.record("detector_error", msg)
        elif "失败" in msg:
            self.record("log_failure", msg)

    # ---- 声明预期 ----
    def allow(self, kind: str, match: str | None = None) -> None:
        """声明某类事件是预期的；`match` 是对 detail 的正则（search），不给则整类放行。"""
        assert kind in KINDS, f"未知 tripwire 种类 {kind!r}"
        self._allowed.append((kind, re.compile(match) if match else None))

    def _is_allowed(self, ev: Event) -> bool:
        return any(k == ev.kind and (p is None or p.search(ev.detail))
                   for k, p in self._allowed)

    # ---- 查询 ----
    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def unexpected(self) -> list[Event]:
        return [e for e in self.events if not self._is_allowed(e)]

    def clear(self) -> None:
        self.events.clear()

    def report(self) -> str:
        bad = self.unexpected()
        lines = [f"tripwire：{len(bad)} 个未声明的事件（用 @pytest.mark.allow(kind) "
                 f"或 tripwire.allow(kind, match=...) 声明预期）："]
        for e in bad[:30]:
            lines.append(f"  - {e}  ← {KINDS.get(e.kind, '')}")
        if len(bad) > 30:
            lines.append(f"  … 另有 {len(bad) - 30} 个")
        return "\n".join(lines)
