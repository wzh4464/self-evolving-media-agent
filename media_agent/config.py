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


# 自演进开关。`off`（默认）= 冻结：`run` 整段跳过演进，不调 LLM、不往 `.agents/`
# 写任何东西；`propose` = 旧行为：找规则盲区 → LLM 提议 → 影子验证 → 写进
# `.agents/rules/` 并挂载（规则带的动作一律不自动执行，待人工确认）。
#
# 为什么默认冻结：生产上 2026-08-20 之后连续 147 轮「提议 0 条」，30 条演进规则
# 全无动作、今天零命中（evolution 调研，2026-09-26），关掉不改变任何行为；
# 而它往仓库目录里写文件，改成按 git tag 部署后会让工作区「脏」——部署的漂移闸门
# 就此拒绝部署，或者更糟，被一次 checkout 覆盖掉。生产行为要能从 git 完整复现。
EVOLVE_MODES = ("off", "propose")


def _evolve_mode(v: str) -> str:
    mode = v.strip().lower()
    if mode not in EVOLVE_MODES:
        # 写错了就大声失败：静默当成 off 会让人以为开着，当成 propose 会往仓库里写文件
        raise ValueError(f"EVOLVE_MODE 只能是 {' / '.join(EVOLVE_MODES)}，收到 {v!r}")
    return mode


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
    evolve_mode: str = "off"
    # qBittorrent 登录成功却报 0 个种子、而媒体库里有视频时，扫描按"数据不完整"处理
    # （见 scan.build_state）。库里确实一个种子都不用的，设 QBIT_ALLOW_EMPTY=1。
    qbit_allow_empty: bool = False
    # 隔离区里的东西至少放这么多天才可能被硬删除，不管判据多有把握（`purge` / `disposal`）。
    # 以前 `purge --apply` 没有下限，一分钟前隔离的也照删，那一批的回退就此失效。`run` 本来
    # 就要等满 TRASH_RETENTION_DAYS；这条管的是 `purge --apply` 的提前放行，以及保留期设得比它短时。
    quarantine_min_age_days: float = 3.0

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
        llm_model=g("LLM_MODEL", "gpt-5.6-luna-2026-07-09"),
        auto_apply=_bool(g("AUTO_APPLY", "false")),
        trash_retention_days=int(g("TRASH_RETENTION_DAYS", "30")),
        max_delete_per_run=int(g("MAX_DELETE_PER_RUN", "50")),
        max_delete_gb_per_run=float(g("MAX_DELETE_GB_PER_RUN", "200")),
        dead_torrent_hours=int(g("DEAD_TORRENT_HOURS", "48")),
        evolve_mode=_evolve_mode(g("EVOLVE_MODE", "off")),
        qbit_allow_empty=_bool(g("QBIT_ALLOW_EMPTY", "false")),
        quarantine_min_age_days=float(g("QUARANTINE_MIN_AGE_DAYS", "3")),
    )
