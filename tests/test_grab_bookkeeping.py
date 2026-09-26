"""回归测试：抓取的记账必须写成，且不被同轮的 sidecar 写回覆盖。

两个都是 2026-09-26 从审计日志里查出来的真事，共同点是**下载本身没坏，
坏的是记账**——所以从外面看一切正常，只有翻日志才发现。

一、`_op_grab_episode` 最后一行 `resp.status_code` 引用了重构后已不存在的
    变量（`a95cdfc` 把手写 HTTP 换成 `qbit.add_torrent()`，返回 bool）。
    种子加进去了、名也改了、sidecar 也写了，最后写审计时抛 NameError，
    被执行器兜住记成 `failed`。后果不是丢下载，是**丢记账**：
    9-16 到 9-26 共 12 次抓取全部记成失败，一条 `applied` 都没有，
    于是这些抓取**没有 undo 记录、回退不了**。

二、`_op_write_sidecar` 是整份覆盖，payload 却是**诊断阶段**算的快照；
    抓取（op 0）排在它（op 10）之前已经往 sidecar 写了新集，于是被盖掉。
    实测「躲在超市后门抽烟的两人」S01E12 文件已落盘、`have` 仍停在 11，
    同一集被连抓两三次（靠 qBittorrent 的 infohash 去重才没真重下）。

    修法是只并回**本轮自己抓的**那几集。不能无脑并磁盘旧值：sidecar-sync
    的职责之一正是把已删掉的集从 `have` 里摘掉，求并集会让它再也摘不掉——
    这条由第 4 项守住。

跑法：.venv/bin/python tests/test_grab_bookkeeping.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_agent import sidecar as sc_mod
from media_agent.actions import Executor
from media_agent.kernel import Action, Finding

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("%-54s %s" % (name, "PASS" if ok else "FAIL"))
    if detail:
        print("    " + detail)
    if not ok:
        failures.append(name)


class FakeQbit:
    def __init__(self):
        self.added = []

    def add_torrent(self, blob, **kw):
        self.added.append(kw)
        return True

    def files(self, h):
        return []


class FakeCfg:
    def __init__(self, root: Path):
        self.audit_log = root / "audit.jsonl"
        self.trash_dir = root / "trash"
        self.max_delete_per_run = 50
        self.max_delete_gb_per_run = 200.0


class FakeCtx:
    def __init__(self, root: Path):
        self.config = FakeCfg(root)
        self.qbit = FakeQbit()
        self.ab = None
        self.logs = []

    def log(self, m):
        self.logs.append(m)


def sidecar_finding(show_dir: Path, payload: dict) -> Finding:
    return Finding(rule="sidecar-sync", kind="sidecar_stale", severity="minor",
                   summary="采集档案需更新", show=show_dir.name,
                   action=Action(op="write_sidecar",
                                 args={"show_dir": str(show_dir), "payload": payload}))


def main() -> int:
    root = Path(tempfile.mkdtemp())
    show = root / "躲在超市后门抽烟的两人"
    (show / "Season 1").mkdir(parents=True)
    ctx = FakeCtx(root)
    ex = Executor(ctx, dry_run=False)

    # 抓取动作只能走真实的 `_op_grab_episode`（要下 .torrent），这里直接模拟
    # 它对执行器状态的两个副作用：写 sidecar、登记"本轮抓了这一集"。
    sc = sc_mod.Sidecar(canonical_title=show.name)
    sc.seasons["1"] = {"have": [10, 11, 12]}
    sc_mod.save(show, sc)
    ex._grabbed[str(show)] = {(1, 12)}

    # sidecar-sync 的 payload 是诊断阶段算的，那时 E12 还没抓 —— have 到 11。
    stale = {"canonical_title": show.name, "seasons": {"1": {"have": [10, 11]}}}
    ex.apply([sidecar_finding(show, stale)])

    after = sc_mod.load(show)
    have = (after.seasons.get("1") or {}).get("have") or []
    check("本轮抓的集不被旧快照盖掉", 12 in have, "have=%s" % have)
    check("快照里本来就有的集保留", {10, 11} <= set(have), "have=%s" % have)

    # 反向守卫：**没被本轮抓过**的集，该由 sidecar-sync 摘掉就得摘掉，
    # 否则删掉一集之后 `have` 永远挂着它，抓取器再也不会补回来。
    sc = sc_mod.Sidecar(canonical_title=show.name)
    sc.seasons["1"] = {"have": [10, 11, 99]}
    sc_mod.save(show, sc)
    ex._grabbed[str(show)] = set()
    ex.apply([sidecar_finding(show, {"canonical_title": show.name,
                                     "seasons": {"1": {"have": [10, 11]}}})])
    have = (sc_mod.load(show).seasons.get("1") or {}).get("have") or []
    check("已经不存在的集仍会被摘掉（不是无脑并集）", 99 not in have, "have=%s" % have)

    # 别名只增不减：抓取时记下的发布名不能被快照冲掉。
    sc = sc_mod.Sidecar(canonical_title=show.name)
    sc.add_alias("Super no Ura de Yani Suu Futari")
    sc_mod.save(show, sc)
    ex._grabbed[str(show)] = set()
    ex.apply([sidecar_finding(show, {"canonical_title": show.name, "aliases": []})])
    check("抓取记下的别名不被冲掉",
          "Super no Ura de Yani Suu Futari" in sc_mod.load(show).aliases)

    # `_op_grab_episode` 末尾那行审计不能再引用不存在的变量。用源码核对——
    # 真跑一遍要下 .torrent、连 qBittorrent，代价远大于收益。
    src = (Path(__file__).resolve().parent.parent
           / "media_agent" / "actions.py").read_text()
    body = src[src.index("def _op_grab_episode"):src.index("def _infohash_v1")]
    check("抓取动作里不再引用已删除的 resp", "resp." not in body)
    check("抓取成功会写 applied 审计（才有 undo 可回退）",
          '"applied"' in body and '"op": "ungrab_episode"' in body)

    print()
    if failures:
        print("FAILED %d: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
