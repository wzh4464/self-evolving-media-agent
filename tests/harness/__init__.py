"""离线测试基座。`pyproject.toml` 把 `tests/` 放进了 pythonpath，所以测试里直接：

    from harness import LibraryBuilder, video, weekly, MikanItem, days_ago

conftest.py 提供的 fixture（都是函数级）：

| fixture | 是什么 |
|---|---|
| `lib` | `LibraryBuilder`，已接好下面所有替身 |
| `tripwire` | 自动启用；测试结束时有未声明事件就判失败 |
| `web` | `FakeWeb`，已替换 `urllib.request.urlopen` |
| `fake_probe` | `FakeProbe`，已替换 `media_agent.probe._run`（`@pytest.mark.ffmpeg` 的测试除外） |
| `project_root` | 临时 PROJECT_ROOT；`state/` 在它下面 |
| `make_file` | 造一个真文件 + 对应的 `MediaFile`（单元测试用） |

marker：`live`（只读核对生产库，默认不跑）、`ffmpeg`（要真 ffprobe）、
`allow(*kinds, match=None)`（声明预期的 tripwire 事件，种类见 `tripwire.KINDS`）。
"""
from .ab import FakeAB, make_ab_db
from .library import Cycle, DirBuilder, LibraryBuilder, ShowBuilder, TorrentHandle
from .llm import FakeLLM
from .probe import FakeProbe, ProbeSpec, video
from .qbit import FakeQbit
from .tmdb import FakeTMDB, weekly
from .torrentfile import bencode, bdecode, make_torrent, read_ident, write_sparse
from .tripwire import KINDS, Tripwire
from .web import MIKAN, FakeWeb, MikanItem, days_ago

__all__ = [
    "Cycle", "DirBuilder", "FakeAB", "FakeLLM", "FakeProbe", "FakeQbit", "FakeTMDB",
    "FakeWeb", "KINDS", "LibraryBuilder", "MIKAN", "MikanItem", "ProbeSpec",
    "ShowBuilder", "TorrentHandle", "Tripwire", "bdecode", "bencode", "days_ago",
    "make_ab_db", "make_torrent", "read_ident", "video", "weekly", "write_sparse",
]
