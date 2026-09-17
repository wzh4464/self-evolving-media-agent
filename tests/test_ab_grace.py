"""回归测试：AutoBangumi 已订阅的番，新集要先留给它，media-agent 只补位。

2026-09-17 查明「每轮诊断都有未改名」的根因：media-agent 的
`episode-available` 和 AutoBangumi 的 RSS 订阅在并行抓同一集的不同发布。
35 部活跃 AB 订阅里，media-agent 近 12 天抓过的 15 部有 14 部同时也在
AB 订阅中——两套抓取器互不知情，同一集必然被抓两遍。

实测的时间差（谁先谁后随机，但集位只有一个）：

    幼女战记 S2E10   media-agent 09-10 02:55:30 → AB 09-10 11:11:20  (+8h16m)
    黄泉的使者 S1E23  media-agent 09-13 03:44:52 → AB 09-13 09:54:05  (+6h09m)

后果是一条固定的噪音链：两份撞同一个目标文件名 → AB 每 60 秒重试改名并
每次记成功（日志里一条重试了 5154 次）→ media-agent 报「集位被占」→
下一轮判重删掉输家才腾出集位。9 月以来判重删了 31 份，约等于一半的
下载量直接扔掉。

`_inflight` 守卫挡不住这个：我们抓的那一刻 AB 的种子还不存在。
唯一能挡住的是**时间**——让出一个宽限窗口。

跑法：.venv/bin/python tests/test_ab_grace.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_agent.plugins.grab import ab_holds_episode

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("%-58s %s" % (name, "PASS" if ok else "FAIL"))
    if detail:
        print("    " + detail)
    if not ok:
        failures.append(name)


TODAY = date(2026, 9, 17)
SUBSCRIBED = {"id": 28, "official_title": "幼女战记", "deleted": 0}


def main() -> int:
    # 1) 核心行为：AB 订阅着的番，当天播出的集不抢。
    check("当天播出 + AB 有订阅 → 让给 AB",
          ab_holds_episode(SUBSCRIBED, "2026-09-17", TODAY, 24.0) is True)

    # 2) 宽限期一过就补位——AB 可能压根没匹配上（字幕组改了发布名、
    #    filter 挡掉了），不能无限等。
    check("播出满 24 小时 AB 仍没动静 → media-agent 出手",
          ab_holds_episode(SUBSCRIBED, "2026-09-16", TODAY, 24.0) is False)
    check("播出多日 → 一定出手",
          ab_holds_episode(SUBSCRIBED, "2026-09-01", TODAY, 24.0) is False)

    # 3) 没有 AB 订阅的番，行为完全不变：谁先出要谁，当天就抓。
    #    辉夜大小姐的 Season 0 特典就属于这类，AB 从来没管过。
    check("无 AB 订阅 → 当天就抓，不受影响",
          ab_holds_episode(None, "2026-09-17", TODAY, 24.0) is False)

    # 4) 已被 AB 删除的订阅不算数——那是人工退订过的，AB 不会再抓。
    check("订阅已删除 → 不让",
          ab_holds_episode({"id": 28, "deleted": 1}, "2026-09-17", TODAY, 24.0)
          is False)

    # 5) 拿不到播出日期时不能瞎让，否则这集永远没人抓。
    check("播出日期缺失 → 不让（宁可重复也不能漏）",
          ab_holds_episode(SUBSCRIBED, None, TODAY, 24.0) is False)
    check("播出日期格式非法 → 不让",
          ab_holds_episode(SUBSCRIBED, "不是日期", TODAY, 24.0) is False)

    # 6) 宽限期可配置：设成 0 等于关掉这个行为，回到今天的抢跑模式。
    check("宽限期 0 → 退回旧行为",
          ab_holds_episode(SUBSCRIBED, "2026-09-17", TODAY, 0.0) is False)
    check("宽限期 72 小时 → 两天前播的也还在窗口内",
          ab_holds_episode(SUBSCRIBED, "2026-09-15", TODAY, 72.0) is True)

    # 7) TMDB 偶尔会给出未来日期（预告排期）。那种集还没播，
    #    抓取器本来就不会提议，这里只要不炸即可。
    check("未来日期 → 让（还没播，等于不抓）",
          ab_holds_episode(SUBSCRIBED, "2026-12-01", TODAY, 24.0) is True)

    print()
    if failures:
        print("FAILED %d: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
