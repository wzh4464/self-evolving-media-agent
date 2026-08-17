"""配置加载：从 .env 读取，环境变量优先。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        # 已存在的环境变量优先
        os.environ.setdefault(k, v)


def _bool(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    media_root: Path
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    ab_url: str
    ab_user: str
    ab_pass: str
    ab_db: Path
    ab_container: str
    docker_bin: str
    tmdb_api_key: str
    tmdb_lang: str
    llm_base: str
    llm_key: str
    llm_model: str
    auto_apply: bool
    trash_retention_days: int
    max_delete_per_run: int
    max_delete_gb_per_run: float
    dead_torrent_hours: int

    @property
    def state_dir(self) -> Path:
        d = PROJECT_ROOT / "state"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def trash_dir(self) -> Path:
        d = self.state_dir / "trash"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def audit_log(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def cache_db(self) -> Path:
        return self.state_dir / "cache.sqlite3"


def load_config(env_file: Path | None = None) -> Config:
    _load_dotenv(env_file or PROJECT_ROOT / ".env")
    g = os.environ.get
    return Config(
        media_root=Path(g("MEDIA_ROOT", "/Volumes/Backup/webdav/Media")),
        qbit_url=g("QBIT_URL", "http://127.0.0.1:1122").rstrip("/"),
        qbit_user=g("QBIT_USER", ""),
        qbit_pass=g("QBIT_PASS", ""),
        ab_url=g("AB_URL", "http://127.0.0.1:7892").rstrip("/"),
        ab_user=g("AB_USER", ""),
        ab_pass=g("AB_PASS", ""),
        ab_db=Path(g("AB_DB", "")),
        ab_container=g("AB_CONTAINER", "autobangumi"),
        docker_bin=g("DOCKER_BIN", "/usr/local/bin/docker"),
        tmdb_api_key=g("TMDB_API_KEY", ""),
        tmdb_lang=g("TMDB_LANG", "zh-CN"),
        llm_base=g("LLM_BASE", "https://api.openlux.ai").rstrip("/"),
        llm_key=g("LLM_KEY", ""),
        llm_model=g("LLM_MODEL", "deepseek-v4-pro-0813"),
        auto_apply=_bool(g("AUTO_APPLY", "false")),
        trash_retention_days=int(g("TRASH_RETENTION_DAYS", "30")),
        max_delete_per_run=int(g("MAX_DELETE_PER_RUN", "50")),
        max_delete_gb_per_run=float(g("MAX_DELETE_GB_PER_RUN", "200")),
        dead_torrent_hours=int(g("DEAD_TORRENT_HOURS", "48")),
    )
