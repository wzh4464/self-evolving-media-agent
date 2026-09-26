"""命令行入口。

    media-agent scan      # 只扫描，看看库里现在什么样
    media-agent diagnose  # 跑全部规则，出问题清单（不改动任何东西）
    media-agent apply     # 执行修复（--dry-run 预演）
    media-agent evolve    # 找规则盲区 → 提议新规则 → 验证 → 提升（需 EVOLVE_MODE=propose）
    media-agent run       # 一轮完整自治：diagnose → apply → [evolve] → 清理隔离区
                          # evolve 只在 EVOLVE_MODE=propose 时跑，默认 off（见 config.py）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import __version__, disposal, runlock
from .actions import Executor, new_run_id
from .cache import Cache
from .clients import (
    AniListClient, AutoBangumiClient, AutoBangumiDB, LLMClient, QBitClient, TMDBClient,
)
from .config import load_config
from .evolution import Evolver, find_failure_patterns, find_residue, load_evolved
from .kernel import Context, Registry
from .plugins import register_builtins
from .runlock import LOCK_NAME, RunLock
from .scan import build_state


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# qBittorrent 不可用或本轮读不全、因此整批拒绝改动时的退出码。
# 以前 `run` 永远返回 0：2026-09-19 那轮登录超时照样执行、删错了东西，
# launchd 上看 last exit code 仍是 0。非零才会被看见。
EXIT_DEGRADED = 3

# 另一个进程持有运行锁、本次什么都没做时的退出码。取 sysexits 的 EX_TEMPFAIL，
# 与 /usr/bin/lockf 等锁超时的退出码一致：launchd 的 last exit code 75 = "被挡住了，下轮再来"。
EXIT_LOCKED = 75


def _needs_lock(args) -> bool:
    """会改动媒体库 / qBittorrent / 状态的子命令才拿运行锁（见 runlock.py）。

    声明在各子命令的 `set_defaults(lock=...)` 上；将来的抓取模式同样要声明。
    """
    lock = getattr(args, "lock", False)
    return bool(lock(args) if callable(lock) else lock)


def _locked_out(holder: str) -> int:
    msg = (f"⏳ 另一个 media-agent 进程正持有运行锁（{holder or '持有者未知，可能是部署脚本'}），"
           "本次不执行任何操作")
    print(f"\n═══ {msg} ═══")
    _log(msg)
    return EXIT_LOCKED


def _refuse(why: str) -> int:
    """大声报告"本轮拒绝改动"，stdout（进 run.log）与 stderr 各一份。"""
    msg = f"⛔ 拒绝执行任何改动：{why}"
    print(f"\n═══ {msg} ═══")
    _log(msg)
    return EXIT_DEGRADED


def _warn_degraded(state) -> None:
    """只读命令也要提醒：种子视图不完整时，下面的结论里有种子的文件会被当成本地文件。"""
    if state.qbit_errors:
        print(f"⚠️  qBittorrent 数据不完整（{len(state.qbit_errors)} 处）：{state.qbit_errors[0]}"
              "——以下结论不可作为改动依据")


def build_context(cfg, need_llm: bool = False) -> Context:
    qbit = None
    try:
        qbit = QBitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    except Exception as e:
        _log(f"⚠️  qBittorrent 连接失败：{e}")

    ab = None
    try:
        if cfg.ab_user:
            ab = AutoBangumiClient(cfg.ab_url, cfg.ab_user, cfg.ab_pass)
    except Exception as e:
        _log(f"⚠️  AutoBangumi API 连接失败（不影响只读诊断）：{e}")

    abdb = AutoBangumiDB(cfg.ab_db, cfg.ab_container, cfg.docker_bin) if cfg.ab_db else None
    tmdb = TMDBClient(cfg.tmdb_api_key, cfg.tmdb_lang)
    if not tmdb.enabled:
        _log("⚠️  未配置 TMDB_API_KEY，标题对齐相关规则将跳过")

    llm = LLMClient(cfg.llm_base, cfg.llm_key, cfg.llm_model)
    if need_llm and not llm.enabled:
        _log("⚠️  未配置 LLM_KEY，自演进与模糊匹配将跳过")

    return Context(cfg, qbit=qbit, ab=ab, abdb=abdb, tmdb=tmdb,
                   anilist=AniListClient(), llm=llm, logger=_log)


def build_registry() -> Registry:
    reg = Registry()
    register_builtins(reg)
    n = load_evolved(reg)          # 演进出的规则挂在内置之后（内置优先）
    if n:
        _log(f"已挂载 {n} 条演进规则")
    return reg


def _print_findings(findings, as_json: bool) -> None:
    if as_json:
        print(json.dumps([f.to_dict() for f in findings], ensure_ascii=False, indent=2))
        return
    if not findings:
        print("✅ 没有发现问题")
        return
    by_kind = Counter(f.kind for f in findings)
    print(f"\n发现 {len(findings)} 个问题：")
    for kind, n in by_kind.most_common():
        print(f"  {kind}: {n}")
    print()
    icon = {"critical": "🔴", "important": "🟡", "minor": "⚪"}
    cur = None
    for f in findings:
        if f.show != cur:
            cur = f.show
            print(f"\n【{cur or '(无归属)'}】")
        print(f"  {icon.get(f.severity,'·')} [{f.rule}] {f.summary}")


def cmd_scan(args, cfg) -> int:
    ctx = build_context(cfg)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    _warn_degraded(state)
    print(f"番剧目录: {len(state.shows)}")
    print(f"文件总数: {sum(len(s.files) for s in state.shows)}")
    print(f"qBittorrent 种子: {len(state.torrents)}")
    print(f"AutoBangumi 记录: {len(state.bangumi_rows)}")
    matched = sum(1 for s in state.shows if s.tmdb_id)
    print(f"TMDB 已匹配: {matched}/{len(state.shows)}")
    if args.verbose:
        for s in state.shows:
            tag = f" → TMDB:{s.tmdb_title}" if s.tmdb_title else ""
            print(f"  {s.dir_name} ({len(s.files)} 文件){tag}")
    return 0


def cmd_diagnose(args, cfg) -> int:
    ctx = build_context(cfg)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    findings = build_registry().run_all(ctx, state)
    if not args.json:
        _warn_degraded(state)
    _print_findings(findings, args.json)

    residue = find_residue(state, findings)
    if residue and not args.json:
        print(f"\n🔍 规则盲区：{len(residue)} 簇未被任何规则解释")
        for r in residue[:5]:
            print(f"  ×{r.count}  {r.samples[0]['filename'] if r.samples else r.signature}")
        print("  运行 `media-agent evolve` 让 agent 尝试为它们立规则")
    return 0


def cmd_apply(args, cfg) -> int:
    ctx = build_context(cfg)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    findings = build_registry().run_all(ctx, state)

    if args.kind:
        findings = [f for f in findings if f.kind in args.kind]
    if args.show:
        findings = [f for f in findings if f.show in args.show]
    if args.limit:
        findings = findings[:args.limit]

    dry = args.dry_run or not cfg.auto_apply
    ex = Executor(ctx, dry_run=dry)
    report = ex.apply(findings)
    if report.refused:
        return _refuse(report.refused)

    print(("【预演】" if dry else "【已执行】") + report.summary()
          + (f"   批次 ID: {ex.run_id}" if not dry else ""))
    for rec in report.applied:
        print(f"  ✅ [{rec['op']}] {rec['summary']}")
    for rec in report.skipped:
        print(f"  ⏭️  [{rec['op']}] {rec['summary']} —— {rec.get('reason','')}")
    for rec in report.failed:
        print(f"  ❌ [{rec['op']}] {rec['summary']} —— {rec.get('error','')}")
    return 0


def cmd_runs(args, cfg) -> int:
    """列出历史批次，供选择回退目标。"""
    ex = Executor(Context(cfg, logger=_log), dry_run=True)
    runs = ex.list_runs()
    if not runs:
        print("还没有任何已执行的批次")
        return 0
    print(f"{'批次 ID':<26} {'时间':<20} {'已执行':>6} {'可回退':>6}  类型")
    for r in runs:
        mark = " ↩已回退" if r.get("rolled_back") else ""
        print(f"{r['run_id']:<26} {r['ts']:<20} {r['applied']:>6} {r['undoable']:>6}  "
              f"{','.join(r['kinds'][:3])}{mark}")
    print(f"\n回退最近一次： media-agent rollback --last")
    return 0


def cmd_rollback(args, cfg) -> int:
    ctx = build_context(cfg)
    ex = Executor(ctx, dry_run=args.dry_run)

    run_id = args.run
    if args.last or not run_id:
        runs = [r for r in ex.list_runs() if not r.get("rolled_back") and r["undoable"]]
        if not runs:
            print("没有可回退的批次")
            return 1
        run_id = runs[-1]["run_id"]

    print(("【预演回退】" if args.dry_run else "【回退】") + f"批次 {run_id}")
    res = ex.rollback(run_id)
    if res.get("refused"):
        return _refuse(res["refused"])
    print(f"  已还原: {res['reverted']}")
    print(f"  跳过:   {res['skipped']}")
    print(f"  失败:   {res['failed']}")
    if res["irreversible"]:
        print(f"  ⚠️  不可逆: {res['irreversible']} 项（当初未记录逆操作）")
    if res["torrent_records_lost"]:
        print(f"  ⚠️  种子记录已丢失: {res['torrent_records_lost']} 项"
              f"（文件可还原，但需重新添加种子才能继续做种）")
    for d in res["skipped_detail"]:
        print(f"    ⏭️  {d.get('skip_reason','')}")
    for d in res["failed_detail"]:
        print(f"    ❌ {d.get('error','')}")
    return 0


def cmd_repair(args, cfg) -> int:
    """修复目录改名后新旧并存的分裂状态。"""
    ctx = build_context(cfg)
    ex = Executor(ctx, dry_run=args.dry_run)
    res = ex.repair_split_dirs(args.run)
    if res.get("refused"):
        return _refuse(res["refused"])
    print(("【预演】" if args.dry_run else "【修复】") + f"分裂目录 {res['pairs']} 对")
    errors = 0
    for d in res["detail"]:
        if args.dry_run:
            print(f"  {d['old']} → {d['new']}: "
                  f"qBit 搬 {d['would_move_via_qbit']}，文件系统搬 {d['would_move_via_fs']}")
        elif d.get("error"):
            errors += 1
            print(f"  ❌ {d['old']} → {d['new']}: {d['error']}")
        else:
            mark = "✅" if d["old_removed"] else "⚠️ 旧目录未清空"
            print(f"  {mark} {d['old']} → {d['new']}: "
                  f"qBit {d['moved_via_qbit']} + 文件系统 {d['moved_via_fs']}"
                  + (f"，{d['stranded']} 个同名滞留" if d["stranded"] else "")
                  + (f"，{d['left_for_torrents']} 个文件仍归种子、没用文件系统搬"
                     if d.get("left_for_torrents") else ""))
    return 1 if errors else 0


def cmd_purge(args, cfg) -> int:
    """清理隔离区里「绝对没问题」的那些。判定条件见 purge.py 的模块注释。"""
    from .purge import build_pool

    ctx = build_context(cfg)
    if ctx.qbit is None:
        # 没有 qBit，build_pool 拿不到任何"种子声明大小 / 进度"的证据（critic N3），
        # 幸存者只能退回时长旁证，判定会偏松。硬删除没有下一层保险，拒绝。
        if args.apply:
            return _refuse("qBittorrent 不可用：拿不到种子证据，不做不可逆删除")
        print("⚠️  qBittorrent 不可用：缺种子证据，以下判定偏松，仅供参考\n")
    pool = build_pool(cfg, ctx.qbit, ctx.tmdb)
    ok = [c for c in pool if c.eligible]
    no = [c for c in pool if not c.eligible]
    free = sum(c.size for c in ok)

    print(f"隔离区共 {len(pool)} 份文件，"
          f"可安全删除 {len(ok)} 份（{free / 2**30:.2f} GB）\n")
    for c in ok:
        print(f"  ✓ {c.trash_path.name[:56]}")
        print(f"      {c.why}")
    if no and args.verbose:
        print(f"\n保留 {len(no)} 份：")
        import collections
        by = collections.Counter(c.why.split("（")[0].split("——")[0] for c in no)
        for why, n in by.most_common():
            print(f"  ×{n:<4} {why}")
    elif no:
        print(f"\n保留 {len(no)} 份（加 --verbose 看原因）")

    if not ok:
        if args.apply:
            n = disposal.sweep_empty_dirs(cfg.trash_dir)
            if n:
                print(f"\n清掉 {n} 个空目录")
        return 0
    if not args.apply:
        print("\n【预演】未删除任何东西。确认无误后加 --apply 执行。")
        return 0

    # 不可逆删除必须留痕，而且**先留痕、后删**（disposal 模块文档）：以前先 unlink 再写
    # 日志，日志写失败就报成"删除失败"，而文件其实已经没了。
    log = disposal.PurgeLog(Path(cfg.state_dir) / disposal.LOG_NAME, new_run_id())
    disposal.recover(log)
    gone = freed = 0
    for c in ok:
        why = disposal.hard_delete(
            log, c.trash_path, c.size, mode="manual", rule=c.rule, origin=c.origin,
            slot=list(c.slot) if c.slot else None,
            survivor=str(c.survivor) if c.survivor else None, reason=c.why)
        if why:
            print(f"  !! 删除失败 {c.trash_path.name}: {why}")
            continue
        gone += 1
        freed += c.size
    print(f"  记录写入 {log.path}（批次 {log.run_id}）")
    n = disposal.sweep_empty_dirs(cfg.trash_dir)
    print(f"\n已删除 {gone} 份，释放 {freed / 2**30:.2f} GB，清掉 {n} 个空目录")
    return 0


def cmd_evolve(args, cfg) -> int:
    if cfg.evolve_mode != "propose":
        # 手动 evolve 同样往 .agents/ 写规则和笔记——冻结期间工作区要与部署的 tag 一致
        print(f"演进已冻结（EVOLVE_MODE={cfg.evolve_mode}）。确要手动演进："
              "EVOLVE_MODE=propose media-agent evolve，产出的规则须提交入库再部署")
        return 1
    ctx = build_context(cfg, need_llm=True)
    if not ctx.llm.enabled:
        print("未配置 LLM_KEY，无法演进")
        return 1

    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    if state.qbit_errors:
        # 与 run 的演进重扫同理：残缺快照里的"盲区"是假的，规则上线却是永久的
        return _refuse(f"qBittorrent 数据不完整，不据此演进规则：{state.qbit_errors[0]}")
    reg = build_registry()
    findings = reg.run_all(ctx, state)

    evolver = Evolver(ctx, reg)
    results = evolver.evolve(state, findings, max_proposals=args.max_proposals)

    if not results:
        print("✅ 没有发现规则盲区，无需演进")
    for r in results:
        outcome = r["outcome"]
        if outcome == "promoted":
            print(f"🎉 新规则已上线: {r['rule_id']}")
            print(f"   命中 {r['validation']['hits']} 个，零误伤")
            print(f"   Agent Note: {r['note']}")
        elif outcome == "rejected":
            print(f"🚫 提议被驳回: {r['rule_id']}")
            for reason in r["validation"].get("reject_reasons", []):
                print(f"   - {reason}")
            print(f"   记录: {r['note']}")
        else:
            print(f"·  {r.get('detail', outcome)}")

    failures = find_failure_patterns(cfg.audit_log)
    if failures:
        print("\n⚠️  反复失败的动作（可能是规则本身有问题）：")
        for f in failures:
            print(f"  {f['runs']} 个批次 / 共 {f['count']} 次 "
                  f"[{f['rule']}] {f['op']}: {f['error']}")
    return 0


def cmd_run(args, cfg) -> int:
    """一轮完整自治。"""
    ctx = build_context(cfg, need_llm=True)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    reg = build_registry()

    findings = reg.run_all(ctx, state)
    print(f"═══ 诊断：{len(findings)} 个问题 ═══")
    _warn_degraded(state)
    _print_findings(findings, False)

    dry = args.dry_run or not cfg.auto_apply
    ex = Executor(ctx, dry_run=dry)
    report = ex.apply(findings)
    if report.refused:
        # fail closed：不修、不演进（演进器会拿这份残缺快照去立规则）、
        # 也不做隔离区的时间清理——降级的一轮不改动任何东西。
        return _refuse(report.refused)
    print(f"\n═══ 修复：{report.summary()} ═══")

    rc = 0
    if cfg.evolve_mode != "propose":
        # 冻结：不重扫、不调 LLM、不构造 Evolver（它的 __init__ 就会建 .agents/rules）
        print(f"\n═══ 演进：已冻结（EVOLVE_MODE={cfg.evolve_mode}） ═══")
    elif ctx.llm.enabled and not args.no_evolve:
        # 修复后重新扫描，残留才是真盲区
        state2 = build_state(ctx, resolve_tmdb=False)
        if state2.qbit_errors:
            # 种子视图残缺时，有种子的文件全变成"无主文件"，演进器会拿它们当
            # 盲区去立规则——规则一旦上线就是永久的。宁可这轮不演进。
            print(f"\n═══ 演进：跳过——重扫时 qBittorrent 数据不完整："
                  f"{state2.qbit_errors[0]} ═══")
            rc = EXIT_DEGRADED
        else:
            findings2 = reg.run_all(ctx, state2)
            results = Evolver(ctx, reg).evolve(state2, findings2,
                                               max_proposals=args.max_proposals)
            promoted = [r for r in results if r["outcome"] == "promoted"]
            print(f"\n═══ 演进：提议 {len(results)} 条，上线 {len(promoted)} 条 ═══")
            for r in promoted:
                print(f"  🎉 {r['rule_id']}")

    purge = ex.purge_trash()
    if purge["purged_files"]:
        print(f"\n═══ 隔离区：清理 {purge['purged_files']} 个过期文件，"
              f"释放 {purge['freed_bytes']/1e9:.1f}GB ═══")
    return rc


def main() -> int:
    p = argparse.ArgumentParser(prog="media-agent", description="番剧媒体库自治 agent")
    # deploy.sh 切换完用它确认"磁盘上这份代码"是哪个版本
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--no-tmdb", action="store_true", help="跳过 TMDB 查询（省时/离线）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描库状态")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("diagnose", help="诊断问题（只读）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diagnose)

    s = sub.add_parser("apply", help="执行修复")
    s.add_argument("--dry-run", action="store_true", help="只预演不实际改动")
    s.add_argument("--kind", nargs="*", help="只处理指定类型的问题")
    s.add_argument("--show", nargs="*", help="只处理指定番剧（目录名）")
    s.add_argument("--limit", type=int, help="最多处理多少条（受控试跑用）")
    s.set_defaults(func=cmd_apply, lock=True)

    s = sub.add_parser("runs", help="列出历史批次（回退用）")
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("rollback", help="一键回退某一批次的全部改动")
    s.add_argument("--run", help="批次 ID，省略则回退最近一次")
    s.add_argument("--last", action="store_true", help="回退最近一次未回退的批次")
    s.add_argument("--dry-run", action="store_true", help="只预演回退，不实际还原")
    s.set_defaults(func=cmd_rollback, lock=True)

    s = sub.add_parser("repair", help="修复目录改名后新旧并存的分裂状态")
    s.add_argument("--run", required=True, help="出问题的批次 ID")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_repair, lock=True)

    s = sub.add_parser("purge", help="清理隔离区里可验证为安全的文件")
    s.add_argument("--apply", action="store_true", help="真正删除（默认只预演）")
    s.add_argument("--verbose", action="store_true", help="列出保留原因")
    # 预演只读隔离区；真删（--apply）才要锁
    s.set_defaults(func=cmd_purge, lock=lambda a: a.apply)

    s = sub.add_parser("evolve", help="自演进：为规则盲区提议新规则")
    s.add_argument("--max-proposals", type=int, default=3)
    # 会写 .agents/，影子验证还会跑全部检测器（含写 sidecar 的抓取规则）
    s.set_defaults(func=cmd_evolve, lock=True)

    s = sub.add_parser("run", help="完整自治轮次")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-evolve", action="store_true",
                   help="本轮不演进（EVOLVE_MODE=propose 时才有意义，默认 off 本来就不跑）")
    s.add_argument("--max-proposals", type=int, default=3)
    s.set_defaults(func=cmd_run, lock=True)

    args = p.parse_args()
    try:
        cfg = load_config()
    except ValueError as e:
        _log(f"配置错误：{e}")
        return 2
    if not _needs_lock(args):
        return args.func(args, cfg)
    lock = RunLock(cfg.state_dir / LOCK_NAME, label=" ".join(["media-agent", *sys.argv[1:]]))
    if not lock.acquire(wait=runlock.DEFAULT_WAIT):
        return _locked_out(lock.holder())
    try:
        return args.func(args, cfg)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
