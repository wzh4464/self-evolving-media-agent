"""生产库上的只读不变量。**默认不跑**。

在 zihan_air 上：

    cd ~/media-agent && MEDIA_AGENT_LIVE=1 ~/.local/bin/uv run --frozen pytest -m live

（需要已同步 dev 依赖组；生产 venv 默认不装 pytest。）

**只允许只读路径**：`build_state(resolve_tmdb=False)` = qBittorrent 的 GET、
AutoBangumi 库 `mode=ro`、磁盘遍历。**不许跑 Registry**——`DuplicateEpisodeDetector`
会打开 `state/cache.sqlite3`，`EpisodeAvailableDetector` 会往真实库里写 sidecar。

原先这一项写在 test_no_phantom_duplicate.py 里，媒体卷不在、或 qBittorrent
登录失败时会拿到空库、空转通过。这里把这两种情况改成明确失败。
"""
import collections

import pytest

pytestmark = pytest.mark.live


def test_scan_emits_no_duplicate_path_entries():
    """2026-09-06 尼古喵喵 S01E08：两个种子宣称同一路径，scan 发出两条条目，
    判重把唯一的真文件移进了隔离区。全库不变量：一个路径只能有一条。"""
    from media_agent.cli import build_context
    from media_agent.config import load_config
    from media_agent.scan import build_state

    cfg = load_config()
    assert cfg.media_root.is_dir(), f"媒体根目录不在：{cfg.media_root}——检查会空转"
    ctx = build_context(cfg)
    assert ctx.qbit is not None, "qBittorrent 登录失败——没有种子，检查会空转"

    state = build_state(ctx, resolve_tmdb=False)
    assert state.torrents, "没读到任何种子——检查会空转"

    dupes, total = [], 0
    for show in state.shows:
        total += len(show.files)
        for path, n in collections.Counter(str(f.path) for f in show.files).items():
            if n > 1:
                dupes.append("×%d %s" % (n, path))
    assert not dupes, "%d 部剧 / %d 条条目；冲突：%s" % (
        len(state.shows), total, "; ".join(dupes[:5]))
