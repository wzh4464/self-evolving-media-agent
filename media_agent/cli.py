"""命令行入口。

    media-agent scan      # 只扫描，看看库里现在什么样
    media-agent diagnose  # 跑全部规则，出问题清单（不改动任何东西）
    media-agent apply     # 执行修复（--dry-run 预演）
    media-agent evolve    # 找规则盲区 → 提议新规则 → 验证 → 提升（需 EVOLVE_MODE=propose）
    media-agent purge     # 隔离区处置预演（--apply 真删，每个都先记 state/purge.jsonl）
    media-agent run       # 一轮完整自治：diagnose → apply → [evolve] → 隔离区处置
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

# 改动照常做了、整轮也跑完了，但有审计记录没能原样写进 audit.jsonl（磁盘满、权限、序列化不了）时的
# 退出码。记录已转写到 stderr（run.err.log）与 state/audit.fallback.jsonl，回退照样读得到；但这种
# 事必须被看见——以前写审计的异常直接冲出执行器、整轮中止（critic N8 的余项），launchd 上只剩一个
# traceback。与 EXIT_DEGRADED（整批拒绝、什么都没改）分开，看退出码就知道是哪一种。
EXIT_AUDIT_INCOMPLETE = 4


def _report_audit_problems(problems: list[str], state_dir) -> bool:
    """审计没能原样写进 audit.jsonl：stdout（run.log）与 stderr（run.err.log）各说一遍。返回是否有问题。"""
    if not problems:
        return False
    from .audit import FALLBACK_NAME
    msg = (f"⚠️  {len(problems)} 条审计没能原样写进 audit.jsonl——已转写到 stderr 与 "
           f"{Path(state_dir) / FALLBACK_NAME}（rollback / runs 会一起读）")
    print(f"\n═══ {msg} ═══")
    for p in problems[:10]:
        print(f"  ⚠️  {p}")
    if len(problems) > 10:
        print(f"  …另 {len(problems) - 10} 条")
    _log(msg)
    return True


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
    if _report_audit_problems(report.audit_problems, cfg.state_dir):
        return EXIT_AUDIT_INCOMPLETE
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
    if res.get("priority_not_restored"):
        print(f"  ⚠️  文件已搬回、合集条目的下载没恢复: {res['priority_not_restored']} 项"
              f"（它此刻没有种子做种）")
        for n in res.get("notes") or []:
            print(f"    ⚠️  {n}")
    for d in res["skipped_detail"]:
        print(f"    ⏭️  {d.get('skip_reason','')}")
    for d in res["failed_detail"]:
        print(f"    ❌ {d.get('error','')}")
    if _report_audit_problems(res.get("audit_problems") or [], cfg.state_dir):
        return EXIT_AUDIT_INCOMPLETE
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


_LABEL = {"duplicate": "判重", "extras": "特典", "dead_partial": "死种半成品",
          "bundled_version": "合并发布的另一版本", "manual": "手动", "other": "其它"}


def _trash_rel(cfg, p) -> str:
    try:
        return str(Path(p).relative_to(cfg.trash_dir))
    except ValueError:
        return str(p)


def _print_overdue(cfg, rep, limit: int = 30) -> None:
    """过了保留期、却没有（也不会）自动删的：逐个报给人。"""
    over = rep.overdue
    if not over:
        return
    print(f"\n═══ 隔离区：{len(over)} 个文件已过保留期 {cfg.trash_retention_days} 天、没有自动删 ═══")
    for c in over[:limit]:
        print(f"  ⏸️  [{_LABEL.get(c.disposition, c.disposition)}] {_trash_rel(cfg, c.trash_path)}"
              f" —— {c.why}")
    if len(over) > limit:
        print(f"  …另 {len(over) - limit} 个（media-agent purge --verbose 看全部）")


def _print_space(cfg, rep) -> None:
    """媒体卷剩余空间低于 MIN_FREE_GB：stdout（run.log）与 stderr 各一份，大声说。"""
    if rep.free_before is None:
        msg = "⚠️  读不到媒体卷的剩余空间（statvfs 失败），容量闸这一轮不起作用"
        print(f"\n{msg}")
        _log(msg)
        return
    if not rep.low_space:
        return
    gb = 1e9
    msg = (f"⚠️  媒体卷剩余 {rep.free_before / gb:.1f} GB，低于 MIN_FREE_GB={cfg.min_free_gb:g} GB"
           f"（隔离区与媒体同一个 APFS 容器，只有硬删除腾空间）")
    print(f"\n═══ {msg} ═══")
    _log(msg)
    if rep.early:
        print(f"  已从最老的开始提前删掉 {len(rep.early)} 个已证明安全的判重"
              f"（{sum(c.size for c in rep.early) / gb:.1f} GB）")
    after = rep.free_after
    if after is not None and after < rep.min_free:
        gone = {id(c) for c in rep.deleted}
        held = sum(c.size for c in rep.pool if id(c) not in gone)
        msg = (f"⚠️  能证明安全的都删了，估计仍只剩 {after / gb:.1f} GB（还差 "
               f"{(rep.min_free - after) / gb:.1f} GB）；隔离区里还有 {held / gb:.1f} GB 证明不了"
               f"可删，需要人处置（media-agent purge --verbose 看每一份的理由）")
        print(f"  {msg}")
        _log(msg)


def _print_disposal(cfg, rep) -> None:
    """`run` 末尾的隔离区处置：删了哪几个、凭什么；过了保留期没删的是哪几个；空间够不够。"""
    if rep.refused:
        print(f"\n═══ 隔离区：未处置——{rep.refused} ═══")
        return
    _print_space(cfg, rep)
    if rep.recovered:
        print(f"\n  ↺ 补完上次中断的 {len(rep.recovered)} 条硬删除记录（{disposal.LOG_NAME}）")
    if rep.deleted:
        by = Counter(_LABEL.get(c.disposition, c.disposition) for c in rep.deleted)
        head = "预演：将硬删除" if rep.dry_run else "硬删除"
        print(f"\n═══ 隔离区：{head} {len(rep.deleted)} 个文件，释放 {rep.freed_bytes / 1e9:.1f}GB"
              f"（{'、'.join(f'{k} {n}' for k, n in by.most_common())}） ═══")
        early = {id(c) for c in rep.early}
        for c in rep.deleted:
            tag = "（空间不足，提前）" if id(c) in early else ""
            print(f"  🗑️  [{_LABEL.get(c.disposition, c.disposition)}]{tag} "
                  f"{_trash_rel(cfg, c.trash_path)} —— {c.why}")
    for c, why in rep.changed:
        print(f"  ⏭️  {_trash_rel(cfg, c.trash_path)} —— 评估之后变了，这次不删：{why}")
    for c, why in rep.failed:
        print(f"  ❌ {_trash_rel(cfg, c.trash_path)} —— 删除失败：{why}")
    _print_overdue(cfg, rep)


def cmd_purge(args, cfg) -> int:
    """隔离区处置的手动入口：默认预演，`--apply` 真删。判据见 purge.py 的模块注释。

    与 `run` 末尾的处置同一套判据（`disposal.dispose`），区别只在模式：这里是人要的，
    证明安全的判重不必等满保留期；特典 / 死种半成品照样要过保留期，要人定的照样不删。
    """
    ctx = build_context(cfg)
    if ctx.qbit is None:
        # 没有 qBit 拿不到任何种子证据（critic N3）。硬删除没有下一层保险，拒绝。
        if args.apply:
            return _refuse("qBittorrent 不可用：拿不到种子证据，不做不可逆删除")
        print("⚠️  qBittorrent 不可用：缺种子证据，以下判定仅供参考\n")
    rep = disposal.dispose(ctx, mode="manual", run_id=new_run_id(), dry_run=not args.apply)
    if rep.refused:
        return _refuse(rep.refused)
    _print_space(cfg, rep)
    pool = rep.pool
    chosen = ({id(c) for c in rep.deleted} | {id(c) for c, _ in rep.failed}
              | {id(c) for c, _ in rep.changed})
    ok = [c for c in pool if id(c) in chosen]
    no = [c for c in pool if id(c) not in chosen]
    size = sum(c.size for c in ok)

    print(f"隔离区共 {len(pool)} 份文件，"
          f"可安全删除 {len(ok)} 份（{size / 2**30:.2f} GB）\n")
    for c in ok:
        print(f"  ✓ [{_LABEL.get(c.disposition, c.disposition)}] {c.trash_path.name[:56]}")
        print(f"      {c.why}")
    if no and args.verbose:
        print(f"\n保留 {len(no)} 份：")
        for c in no:
            print(f"  · [{_LABEL.get(c.disposition, c.disposition)}] "
                  f"{_trash_rel(cfg, c.trash_path)}")
            print(f"      {c.why}")
    elif no:
        by = Counter(_LABEL.get(c.disposition, c.disposition) for c in no)
        print(f"\n保留 {len(no)} 份（{'、'.join(f'{k} {n}' for k, n in by.most_common())}；"
              f"加 --verbose 看原因）")

    if not args.apply:
        if ok:
            print("\n【预演】未删除任何东西。确认无误后加 --apply 执行。")
        return 0
    for c, why in rep.changed:
        print(f"  ⏭️  没删 {c.trash_path.name}：评估之后变了——{why}")
    for c, why in rep.failed:
        print(f"  !! 删除失败 {c.trash_path.name}: {why}")
    if rep.recovered:
        print(f"  ↺ 补完上次中断的 {len(rep.recovered)} 条硬删除记录")
    print(f"  记录写入 {Path(cfg.state_dir) / disposal.LOG_NAME}（批次 {rep.run_id}）")
    print(f"\n已删除 {len(rep.deleted)} 份，释放 {rep.freed_bytes / 2**30:.2f} GB")
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
        # 也不处置隔离区——降级的一轮不改动任何东西。
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

    # 隔离区处置（disposal 模块文档）：以前这里按日期 rmtree 整个日目录、一行记录都不写
    # （Executor.purge_trash，生产上删掉过 6 个文件 4.7GB，说不出是哪几个）。现在按处置类别
    # 逐个判、逐个预写日志后删；要人定的过了保留期只报不删。
    _print_disposal(cfg, disposal.dispose(ctx, mode="run", run_id=ex.run_id, dry_run=dry))
    # 审计写不进去不中止这一轮（上面的处置照跑——磁盘满时它是唯一腾空间的一步），但在最后大声说
    if _report_audit_problems(report.audit_problems, cfg.state_dir):
        return EXIT_AUDIT_INCOMPLETE
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
