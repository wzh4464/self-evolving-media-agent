"""回归测试：同一磁盘路径不得产出重复删除动作。

2026-09-06 尼古喵喵 S01E08 因此丢过一次原件。两个种子宣称同一路径
（换版时旧种子被停用、文件移走，但 qBittorrent 里的记录还在，
新种子 renameFile 到同一集位文件名后两者重合），`scan` 为同一路径
发出两条 MediaFile，`duplicate-episode` 判定"这一集有 2 个文件"，
排序后清理"输的那个"——删掉的正是唯一的真文件。
审计里留下自相矛盾的一行：`保留 X，清理 X`。

跑法：.venv/bin/python tests/test_no_phantom_duplicate.py
"""
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_agent.cli import build_context
from media_agent.config import load_config
from media_agent.kernel import LibraryState, MediaFile, Show
from media_agent.plugins.builtin import DuplicateEpisodeDetector
from media_agent.scan import build_state

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("%-52s %s" % (name, "PASS" if ok else "FAIL"))
    if detail:
        print("    " + detail)
    if not ok:
        failures.append(name)


def main() -> int:
    cfg = load_config()
    ctx = build_context(cfg)

    # 1) 全库不变量：scan 不得为同一路径发出两条条目。
    state = build_state(ctx, resolve_tmdb=False)
    dupes = []
    total = 0
    for show in state.shows:
        total += len(show.files)
        for path, n in collections.Counter(str(f.path) for f in show.files).items():
            if n > 1:
                dupes.append("×%d %s" % (n, path))
    check("scan：全库无同路径重复条目", not dupes,
          "%d 部剧 / %d 条条目；%s" % (
              len(state.shows), total,
              "无冲突" if not dupes else "冲突：" + "; ".join(dupes[:5])))

    # 2) 规则护栏：即使上游真的发出了两条同路径条目，也不能产出删除动作。
    p = Path(cfg.media_root) / "__phantom__" / "Season 1" / "__phantom__ S01E08.mkv"

    def mk(torrent_name: str, size: int, h: str) -> MediaFile:
        return MediaFile(
            path=p, size=size, show_dir="__phantom__", season_dir="Season 1",
            filename=p.name, torrent_hash=h, torrent_name=torrent_name,
            torrent_state="stalledUP", torrent_progress=1.0,
            torrent_tags="", torrent_category="__phantom__",
        )

    show = Show(dir_name="__phantom__", dir_path=p.parent.parent)
    show.files = [
        mk("[GroupA] Show - 08 (1920x1080 AVC AAC).mkv", 745065995, "a" * 40),
        mk("[GroupB] Show - 08 [1080p HEVC-10bit AAC SRTx2].mkv", 593601176, "b" * 40),
    ]
    st = LibraryState()
    st.shows = [show]
    found = list(DuplicateEpisodeDetector().detect(ctx, st))
    dels = [f for f in found if f.kind == "duplicate"]
    check("duplicate-episode：同路径不产出删除动作", not dels,
          "共 %d 条 finding，删除类 %d 条" % (len(found), len(dels)))


    # 3) 重复集取舍：字幕能力必须排在体积之前。
    #    2026-09-05 尼古喵喵 S01E08：带简繁双字幕轨的 HEVC 566MB 输给了
    #    零字幕轨的 AVC 710MB，因为排序键只看文件名、且裸比体积。
    from media_agent.probe import MediaInfo, size_for_compare

    sub_hevc = MediaInfo(vcodec="hevc", height=1080, sub_count=2,
                         sub_marks=("chi 简体中文", "chi 繁體中文"))
    raw_avc = MediaInfo(vcodec="h264", height=1080, sub_count=0, sub_marks=())
    check("probe：带简繁双轨识别为简体",
          sub_hevc.has_simplified and sub_hevc.subtitle_rank() == MediaInfo.SOFT_SIMPLIFIED,
          "subtitle_rank=%d" % sub_hevc.subtitle_rank())
    check("probe：零字幕轨得 0 分", raw_avc.subtitle_rank() == MediaInfo.NONE,
          "subtitle_rank=%d" % raw_avc.subtitle_rank())

    # 2026-09-08 穹庐下的魔女 S01E11：字幕轨 title 写的是 JPSC/JPTC，
    # probe 自造的 `\bsc\b` 匹配不到（JP 和 SC 之间没有词边界），
    # 于是简繁双内封轨只被当成"普通中文"，与靠名字猜的内嵌同分，按体积输掉。
    jpsc = MediaInfo(vcodec="hevc", height=1080, sub_count=2,
                     sub_marks=("chi JPSC", "chi JPTC"))
    check("probe：JPSC/JPTC 轨识别为简体",
          jpsc.has_simplified and jpsc.subtitle_rank() == MediaInfo.SOFT_SIMPLIFIED,
          "sub_marks=%s rank=%d" % (jpsc.sub_marks, jpsc.subtitle_rank()))
    check("排序：探到的内封轨压过靠名字猜的内嵌",
          MediaInfo.SOFT_CHINESE > MediaInfo.HARD_SIMPLIFIED,
          "内封最好、内嵌也行——同分就把这个偏好抹平了")
    check("排序：字幕能力先于体积",
          (1080, sub_hevc.subtitle_rank()) > (1080, raw_avc.subtitle_rank()),
          "带字幕的 HEVC 必须排在无字幕的 AVC 之前")
    # 跨编码体积折算：566MB HEVC 的等效画质应高于 710MB AVC
    hevc_eq = size_for_compare(593601176, "hevc")
    avc_eq = size_for_compare(745065995, "h264")
    check("排序：跨编码体积折算后 HEVC 反超", hevc_eq > avc_eq,
          "HEVC 566MB → %.0fMB 等效；AVC 710MB → %.0fMB 等效"
          % (hevc_eq / 2 ** 20, avc_eq / 2 ** 20))

    print()
    if failures:
        print("失败 %d 项：%s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
