"""`.gitignore` 必须挡住生产机上才有的机密与备份，又不能挡住版本化的行为输入。

生产目录改成 git 工作区之后（deploy/convert-to-git.sh），一次顺手的 `git add -A`
就可能把 `.env.bak-20260917T205051`（真密钥）带进这个**公开**仓库。deploy 调研
（2026-09-26）列出的生产独有文件逐个在这里核对。反过来 `.agents/rules/` 与
`.agents/preferences.json` 是行为输入，被忽略的话生产行为就无法从 git 复现，
部署的漂移闸门也看不见对它们的就地修改。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MUST_IGNORE = [
    ".env",
    ".env.bak-20260917T205051",
    ".env.local",
    "media_agent/actions.py.bak-20260915",
    "media_agent/plugins/builtin.py.bak-20260907",
    ".agents/preferences.json.bak-20260905",
    "qb_downloader.py.bak",
    ".DS_Store",
    "media_agent/.DS_Store",
    ".agents/.DS_Store",
    "state/audit.jsonl",
    "state/trash/2026-09-14/x.mkv",
    "state/run.lock",
    "state/deploy.history",
    ".venv/bin/media-agent",
]
MUST_TRACK = [
    ".env.example",
    ".agents/preferences.json",
    ".agents/rules/vcb-studio-sp-shorts-unrenamed.json",
    ".agents/rules/some-future-rule.json",
    ".agents/notes/implemented/process/2026-08-18-some-note.md",
    "media_agent/cli.py",
    "deploy/deploy.sh",
    "deploy/com.zihan.media-agent.plist",
    "uv.lock",
]


@pytest.fixture
def ignored(tmp_path):
    """返回一个函数：给一批路径，回答其中哪些被忽略（一次 git 调用）。"""
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("需要 git 与仓库的 .git")
    wrapper = tmp_path / "bin" / "git"                  # 子进程守卫只放行 tmp 里的可执行文件
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(f'#!/bin/sh\nexec {git} "$@"\n')
    wrapper.chmod(0o755)

    def check(paths: list[str]) -> set[str]:
        r = subprocess.run([str(wrapper), "-C", str(REPO), "check-ignore", "--no-index",
                            "--stdin"], input="\n".join(paths) + "\n",
                           capture_output=True, text=True)
        assert r.returncode in (0, 1), r.stderr        # 1 = 一个都没被忽略
        return set(r.stdout.splitlines())

    return check


def test_prod_only_secrets_and_backups_are_ignored(ignored):
    assert sorted(set(MUST_IGNORE) - ignored(MUST_IGNORE)) == []


def test_versioned_inputs_are_not_ignored(ignored):
    assert sorted(ignored(MUST_TRACK)) == []
