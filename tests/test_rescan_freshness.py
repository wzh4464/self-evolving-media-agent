"""同一个 Context 上重扫，必须看到的是**此刻**的库，而不是上一次扫描的缓存。

testinfra B3：`ctx._tfile_cache`（种子文件列表）在 Context 的整个生命周期里从不失效。
`cmd_run` 在 apply 之后用同一个 ctx 重扫给演进器用（cli.py 的演进分支）：
改名前的条目名从缓存里出来，成了"种子声明了、盘上没有"的幻影，改名后的真文件
反倒没被任何种子覆盖、成了本地文件——于是同一集出现两份（幻影重复）、还有一条
"未改名"。今天它只喂演进器；任何在同一 ctx 上循环 诊断→执行 的改造都会继承它，
而幻影一旦被 trash 就是 critic N1 的那种记录。

`builtin._OFFSET_CACHE`（sidecar 的 season_offsets）是模块级的，同样从不失效。
"""
from __future__ import annotations

from harness import video
from media_agent.actions import Executor
from media_agent.scan import build_state

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])


def test_rescan_on_the_same_ctx_sees_the_rename(lib):
    s1 = lib.show("尼古喵喵").season(1)
    s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)
    ctx = lib.context()
    reg = lib.registry()

    rep = Executor(ctx, dry_run=False, run_id="t1").apply(reg.run_all(ctx, build_state(ctx)))
    assert [r["via"] for r in rep.applied if r["op"] == "rename"] == ["qbittorrent"]

    state2 = build_state(ctx)                           # 同一个 ctx，模拟 cmd_run 的演进重扫
    paths = [f.path for s in state2.shows for f in s.files]
    assert paths == [s1.path / "尼古喵喵 S01E08.mkv"]  # 没有改名前的幻影
    assert all(f.torrent_hash for s in state2.shows for f in s.files)
    # 没有幻影重复、没有"还没改名"（sidecar-sync 晚一轮写档案是另一回事，不在此列）
    again = reg.run_all(ctx, state2)
    assert not [f for f in again if f.kind in ("duplicate", "unrenamed")]
    assert not [f for f in again if f.action and f.action.op in ("rename", "trash")]


def test_rescan_picks_up_changed_season_offsets(lib):
    """sidecar 的 season_offsets 在两次扫描之间改了（用户照提示补上了换算），
    同一进程里的下一次扫描要用新值。"""
    sh = lib.show("Re:Zero")
    s1 = sh.season(1)
    s1.single("[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 [1080p].mkv",
              size=487_000_000)
    ctx = lib.context()
    reg = lib.registry()

    first = reg.run_all(ctx, build_state(ctx))
    assert "season_numbering_conflict" in [f.kind for f in first]

    sh.sidecar(season_offsets={"3": 50})
    second = reg.run_all(ctx, build_state(ctx))

    assert "season_numbering_conflict" not in [f.kind for f in second]
    [ren] = [f for f in second if f.action and f.action.op == "rename"]
    assert ren.action.args["new_name"] == "Re:Zero S01E58.mkv"
