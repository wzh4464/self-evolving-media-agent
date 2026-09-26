"""media-agent：自治的番剧媒体库治理 agent。"""
from __future__ import annotations


def _version() -> str:
    """版本号：优先读源码树里的 pyproject.toml，读不到再问已安装的元数据。

    生产是可编辑安装。deploy.sh 切到新 tag 之后、`uv sync` 之前，已安装的元数据
    还是上一个版本——而部署要确认的恰恰是"磁盘上这份代码"是哪个版本。
    """
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        import tomllib

        with pyproject.open("rb") as fh:
            project = tomllib.load(fh).get("project", {})
        if project.get("name") == "media-agent" and project.get("version"):
            return str(project["version"])
    except (OSError, ValueError):
        pass
    try:
        from importlib.metadata import version

        return version("media-agent")
    except Exception:           # 未安装（直接从源码目录跑）也不该让 --version 崩
        return "unknown"


__version__ = _version()
