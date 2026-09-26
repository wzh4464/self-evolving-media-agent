"""`media-agent --version`：deploy.sh 切换完用它确认磁盘上这份代码是哪个版本。"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

import media_agent
from media_agent import cli

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_version_comes_from_the_source_tree():
    """可编辑安装切了 tag、还没 uv sync 时，已装元数据是旧的——以源码树为准。"""
    assert media_agent.__version__ == _pyproject_version()


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["media-agent", "--version"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"media-agent {_pyproject_version()}"
