"""回归测试：media-agent 自己挑中并复核通过的那一份，判重不得再换掉。

2026-09-11 的真实误删——抓取器按 preferences 为《尼古喵喵》S01E10 选了
一个合并发布（TV 版 + 邪龙解放版装在同一个种子里，`无修` 命中 +100），
判重随后按画质/体积规则删掉了其中的邪龙解放版。审计里留下：

    保留: 【7月】尼古喵喵 10【TV版】.mp4
    清理: 【7月】尼古喵喵 10【邪龙解放版】.mp4

（更正：2026-09-18 一度以为 E01–E10 因此全成了 TV 版。按 infohash 核对，
库里那十集就是 LoliHouse 的邪竜解放版——它的种子内部名不标版本，
`[LoliHouse] Yani Neko - 08 [...]`，凭名字认不出来。）

根因不是排序写错了：`_rank_for_keep` 比的是画质、字幕轨、体积，
而「无删减」「特定字幕组」这类择源诉求它根本表达不了。让画质规则去
覆盖择源的结论，等于择源白做。所以有 `ma:` 钉子且复核通过的那一份
直接封存集位，不参与排名。

跑法：.venv/bin/python tests/test_seal_slot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_agent import preferences
from media_agent.kernel import MediaFile
from media_agent.plugins.builtin import (_prefer_score, _release_agrees,
                                        meets_requirements)

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("%-56s %s" % (name, "PASS" if ok else "FAIL"))
    if detail:
        print("    " + detail)
    if not ok:
        failures.append(name)


def mkfile(filename: str, torrent_name: str = "", size: int = 646053213,
           category: str = "", tags: str = "") -> MediaFile:
    return MediaFile(
        path=Path("/tmp/does-not-exist") / filename,   # probe 探不到 → 只凭发布名
        size=size, show_dir="尼古喵喵", season_dir="Season 1", filename=filename,
        torrent_name=torrent_name or filename,
        torrent_tags=tags, torrent_category=category,
    )


class FakeShow:
    dir_path = Path("/tmp/does-not-exist")


XIE = ("[LoliHouse] 尼古喵喵 (邪竜解放版) / ヤニねこ / Yani Neko / Chainsmoker Cat"
       " - 10 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]")
TV = "[LoliHouse] Yani Neko - 10 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"


def main() -> int:
    show = FakeShow()

    # 1) 复核：邪竜解放版过硬门槛（名字里有「简繁内封」）。
    ok, why = meets_requirements(mkfile("尼古喵喵 S01E10.mkv", XIE))
    check("邪竜解放版通过复核", ok, why)

    # 2) 复核不是橡皮图章：没有任何中文字幕证据的片源必须被挡下，
    #    这正是 2026-09-05 那个 711MB 零字幕轨的 ABEMA 转载版。
    abema = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV) [0B743B5B]"
    ok, why = meets_requirements(mkfile("尼古喵喵 S01E08.mkv", abema))
    check("ABEMA 纯日语生肉过不了复核", not ok, why)

    # 3) LoliHouse 的 `ASSx2` / `SRTx2` 命名不含任何硬门槛关键词，
    #    「简繁内封字幕」只在 Mikan 站点标题里。**只看名字会把整个组判死**，
    #    所以复核必须先看文件、再看名字。这里用探不到的假路径验证
    #    「名字判死」的那一半确实会发生，真实库上的另一半见下面第 9 项。
    loli = "[LoliHouse] Super no Ura de Yani Suu Futari - 09 [WebRip 1080p HEVC-10bit AAC ASSx2]"
    ok, why = meets_requirements(mkfile("x.mkv", loli))
    check("LoliHouse ASSx2：探不到时确实只能靠名字（会判不合格）", not ok, why)

    # 4) 内嵌硬字幕探不到轨道，不能因此判不合格。
    ok, why = meets_requirements(mkfile(
        "x.mkv", "[樱桃花字幕组]尼古喵喵 Yanineko - 10 [1080p][简日内嵌]"))
    check("内嵌硬字幕仍算合格", ok, why)

    # 5) 发布名确认集位：TV 版的发布名认得出第 10 集 → 可以当轮清理。
    check("TV 版发布名确认 S01E10",
          _release_agrees(mkfile("尼古喵喵 S01E10.mkv", TV), show, 1, 10) is True)

    # 6) 关键守卫：AB 把 `3rd Season - 08` 改成了 `S01E08`，文件名骗人，
    #    发布名不骗人。这种候选不许在封存快车道上被清理——
    #    2026-08-31 的 1.31GB 原片就是这么没的。
    lying = mkfile("Re：从零开始的异世界生活 S01E08.mkv",
                   "[Fyy Raws] Re Zero kara Hajimeru Isekai Seikatsu 3rd Season - 08 "
                   "[WebRip 1080p HEVC-10bit AAC][简繁内封]",
                   category="Bangumi")
    check("AB 改错名的候选：发布名声明第 3 季 → 拒绝确认",
          _release_agrees(lying, show, 1, 8) is False)

    # 7) 集号对不上的也拒绝。
    check("集号不符 → 拒绝确认",
          _release_agrees(mkfile("x.mkv", TV), show, 1, 9) is False)

    # 8) 合集种子的发布名解析不出单集集号 → 拒绝确认，不许当轮删。
    batch = mkfile("x.mkv", "[LoliHouse] Yani Neko [01-12][WebRip 1080p HEVC-10bit AAC]")
    check("合集种子 → 拒绝确认", _release_agrees(batch, show, 1, 10) is False)

    # 9) 没有发布名（纯本地文件）→ 拒绝确认。
    f = mkfile("尼古喵喵 S01E10.mkv")
    f.torrent_name = ""
    check("无发布名 → 拒绝确认", _release_agrees(f, show, 1, 10) is False)

    # 10) 合并发布：一个种子里装着 TV 版 + 邪龙解放版，两个文件共用同一个
    #     torrent_name。必须靠**文件名**分高下，否则并列、随机留下 TV 版——
    #     2026-09-18 EP11 实测就是这么选错的。
    bundle = "[TV版&无修版] 尼古喵喵 - EP11 [简／繁] (1080p H.264 AAC SRTx2)"
    tv = mkfile("【7月】尼古喵喵 11【TV版】.mp4", bundle, size=738434422,
                tags="ab:32, ma:S01E11")
    xie = mkfile("【7月】尼古喵喵 11【邪龙解放版】.mp4", bundle, size=738563085,
                 tags="ab:32, ma:S01E11")
    check("合并发布：邪龙解放版排在 TV 版之前",
          _prefer_score(xie) > _prefer_score(tv),
          "邪龙版 %s  vs  TV版 %s" % (_prefer_score(xie)[:2], _prefer_score(tv)[:2]))
    check("两者复核结论一致（硬门槛看的是种子标题）",
          meets_requirements(tv)[0] == meets_requirements(xie)[0] is True)
    # 体积几乎一样（738.4MB vs 738.6MB），靠体积分不出来——这正是必须
    # 用偏好分的原因。
    check("体积几乎相等，分不出高下",
          abs(tv.size - xie.size) / tv.size < 0.001)

    # 11) 按番指定版本（「只保留邪竜解放版」）。只在抓取选源时把关——那时
    #     手里有 Mikan 的完整标题；下载后种子内部名不标版本，认不出来。
    rules = preferences.with_requirement(
        preferences.load_rules(), ["邪竜解放版", "邪龙解放版"], name="只保留邪竜解放版")
    loli_xie = {"title": "[LoliHouse] 尼古喵喵 (邪竜解放版) / ヤニねこ / Yani Neko / "
                         "Chainsmoker Cat - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"}
    nest = {"title": "[NEST] 尼古喵喵 / ヤニねこ / Chainsmoker Cat - 12 "
                     "[NF WEB-DL 1080p AVC AAC][简繁日内封]"}
    bundle12 = {"title": "[TV版&无修版] 尼古喵喵 - EP12 [简／繁] (1080p H.264 AAC SRTx2)"}
    sfb = {"title": "[SFBgm] 尼古喵喵 / Yani Neko - 12 [WebRip 1080p AVC AAC CHT MP4]"}
    check("NF 删减版分数不低，但过不了按番门槛",
          not preferences.evaluate(nest["title"], rules).acceptable
          and preferences.evaluate(nest["title"]).acceptable)
    check("合并发布也不收（里面带着 TV 版）",
          not preferences.evaluate(bundle12["title"], rules).acceptable)
    best, _ = preferences.pick_best([nest, sfb, bundle12, loli_xie], rules)
    check("四选一只会选 LoliHouse 邪竜解放版", best is loli_xie)
    best, _ = preferences.pick_best([nest, sfb, bundle12], rules)
    check("邪竜版还没出时宁可不抓", best is None)
    check("没设按番门槛的番不受影响",
          preferences.with_requirement(preferences.load_rules(), []) is not None
          and preferences.pick_best([nest], preferences.with_requirement(
              preferences.load_rules(), []))[0] is nest)

    print()
    if failures:
        print("FAILED %d: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
