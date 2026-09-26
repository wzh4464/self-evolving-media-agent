"""GitHub Actions 工作流里引用的 action 必须钉在一个确实存在、不会移动的提交上。

2026-09-26 的 `astral-sh/setup-uv@v10`：setup-uv 从 v8 起不再发布浮动的大版本 tag，
上游只有 `v10.0.0`…`v10.2.0`，没有 `v10` 这个 ref——GitHub Actions 解析不到，
**每个任务都在第一步失败**，包括 macOS 的 bash 3.2 腿与生产 uv 0.7.2 的锁文件任务。
测试全绿、CI 却一次都没跑起来，外面看不出来。

离线测试核实不了"上游有没有这个 ref"，但能守住形状：40 位提交 SHA（不可移动，
也不依赖对方继续发布浮动 tag）+ 行尾 `# vX.Y.Z` 注释（给人看、给升级工具认）。
换版本时先 `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/vX.Y.Z*'`
拿到 SHA 再改——见 `.agents/notes/implemented/testing/2026-09-26-offline-test-harness.md` 的 CI 一节。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")


def _uses(path: Path) -> list[tuple[int, str, str]]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = USES.match(line)
        if m:
            out.append((n, m.group(1), m.group(2)))
    return out


def test_there_is_a_workflow_to_check():
    assert WORKFLOWS and any(_uses(p) for p in WORKFLOWS)


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha_with_a_version_comment(wf):
    bad = []
    for n, ref, rest in _uses(wf):
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        ok_ref = re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)
        ok_comment = re.fullmatch(r"\s+#\s*v\d+\.\d+\.\d+\s*", rest)
        if not (ok_ref and ok_comment):
            bad.append(f"{wf.name}:{n}: {ref}{rest}")
    assert bad == [], "action 要钉到 40 位提交 SHA，并在行尾注明 # vX.Y.Z：\n" + "\n".join(bad)
