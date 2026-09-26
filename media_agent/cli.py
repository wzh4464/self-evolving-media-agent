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
import re
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import __version__, disposal, health, history, notify, runlock, runlog
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

# 异常冲出了这一轮：与 Python 未捕获异常的退出码相同，只是健康报告照写（`cmd_run`）。
EXIT_CRASH = health.EXIT_CRASH

# 这一轮跑完了，但健康报告是 critical 且不属于上面几种：处置之后媒体卷剩余仍低于 MIN_FREE_GB
# （`health.exit_code_for`）。launchd 只记下退出码、照常 6 小时后再起下一轮——非零不会让它停掉任务。
EXIT_CRITICAL = health.EXIT_CRITICAL

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
    # 各客户端此刻的状况，健康报告用（`ok` / `down: 原因` / `off（为什么）`）
    status: dict[str, str] = {}
    qbit = None
    try:
        qbit = QBitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        status["qbit"] = "ok"
    except Exception as e:
        _log(f"⚠️  qBittorrent 连接失败：{e}")
        status["qbit"] = f"down: {type(e).__name__}: {e}"[:200]

    ab = None
    try:
        if cfg.ab_user:
            ab = AutoBangumiClient(cfg.ab_url, cfg.ab_user, cfg.ab_pass)
            status["ab"] = "ok"
        else:
            status["ab"] = "off（未配置 AB_USER）"
    except Exception as e:
        _log(f"⚠️  AutoBangumi API 连接失败（不影响只读诊断）：{e}")
        status["ab"] = f"down: {type(e).__name__}: {e}"[:200]

    abdb = AutoBangumiDB(cfg.ab_db, cfg.ab_container, cfg.docker_bin) if cfg.ab_db else None
    status["abdb"] = "ok" if abdb else "off（未配置 AB_DB）"
    tmdb = TMDBClient(cfg.tmdb_api_key, cfg.tmdb_lang)
    status["tmdb"] = "ok" if tmdb.enabled else "off（未配置 TMDB_API_KEY）"
    if not tmdb.enabled:
        _log("⚠️  未配置 TMDB_API_KEY，标题对齐相关规则将跳过")

    llm = LLMClient(cfg.llm_base, cfg.llm_key, cfg.llm_model)
    status["llm"] = "ok" if llm.enabled else "off（未配置 LLM_KEY）"
    if need_llm and not llm.enabled:
        _log("⚠️  未配置 LLM_KEY，自演进与模糊匹配将跳过")

    ctx = Context(cfg, qbit=qbit, ab=ab, abdb=abdb, tmdb=tmdb,
                  anilist=AniListClient(), llm=llm, logger=_log)
    ctx.client_status = status
    return ctx


def build_registry() -> Registry:
    reg = Registry()
    register_builtins(reg)
    n = load_evolved(reg)          # 演进出的规则挂在内置之后（内置优先）
    if n:
        _log(f"已挂载 {n} 条演进规则")
    for e in reg.load_errors:      # 坏规则文件：以前一声不吭地少挂一条
        _log(f"⚠️  [rules] 演进规则加载失败，没挂上：{e}")
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


def _record_findings(cfg, run_id: str, findings, state, cmd: str) -> None:
    """这一轮的全部发现写进发现历史（`history`，critic N11）。写不进去只在 stderr 说一句：观测不拦正事。"""
    _, problems = history.write_snapshot(cfg.state_dir, run_id, findings, cmd=cmd,
                                         degraded=bool(state.qbit_errors))
    for p in problems:
        _log(f"⚠️  发现历史：{p}")


def cmd_diagnose(args, cfg) -> int:
    ctx = build_context(cfg)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    findings = build_registry().run_all(ctx, state)
    _record_findings(cfg, new_run_id(), findings, state, "diagnose")
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
    _print_unknown(report.unknown)
    if _report_audit_problems(report.audit_problems, cfg.state_dir):
        return EXIT_AUDIT_INCOMPLETE
    return 0


def _print_unknown(recs: list[dict], limit: int = 20) -> None:
    """记 unknown 的动作（改动也许生效了、确认不了）逐条列出：它们要人按此刻状态核对。"""
    for rec in recs[:limit]:
        print(f"  ❓ [{rec['op']}] {rec['summary']} —— 未确认：{rec.get('reason') or rec.get('error', '')}"
              + ("（带逆操作，rollback 会按此刻状态尝试还原）" if rec.get("undo") else ""))
    if len(recs) > limit:
        print(f"  ❓ …另 {len(recs) - limit} 条未确认（media-agent runs / audit.jsonl）")


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
        if r.get("rollback_of"):
            # 回退的逐步记录（每一步还原 / 跳过 / 出错各一条）；它本身不能再回退
            mark += f" ↩回退 {r['rollback_of']} 的记录"
        if r.get("unconfirmed"):
            # 当初记 unknown 的：改动也许生效了。带逆操作的已算进"可回退"，回退时按此刻状态核对
            mark += f" ❓未确认 {r['unconfirmed']}"
        print(f"{r['run_id']:<26} {r['ts']:<20} {r['applied']:>6} {r['undoable']:>6}  "
              f"{','.join(r['kinds'][:3])}{mark}")
    print(f"\n回退最近一次： media-agent rollback --last")
    return 0


def cmd_rollback(args, cfg) -> int:
    ctx = build_context(cfg)
    ex = Executor(ctx, dry_run=args.dry_run)

    run_id = args.run
    if args.last or not run_id:
        runs = [r for r in ex.list_runs()
                if not r.get("rolled_back") and not r.get("rollback_of") and r["undoable"]]
        if not runs:
            print("没有可回退的批次")
            return 1
        run_id = runs[-1]["run_id"]

    print(("【预演回退】" if args.dry_run else "【回退】") + f"批次 {run_id}")
    res = ex.rollback(run_id)
    if res.get("refused"):
        return _refuse(res["refused"])
    if not args.dry_run:
        print(f"  逐步记录：批次 rollback-of-{run_id}（每一步还原 / 跳过 / 出错各一条，media-agent runs 可见）")
    print(f"  已还原: {res['reverted']}")
    print(f"  跳过:   {res['skipped']}")
    print(f"  失败:   {res['failed']}")
    if res["irreversible"]:
        print(f"  ⚠️  不可逆: {res['irreversible']} 项（当初未记录逆操作）")
    if res["torrent_records_lost"]:
        print(f"  ⚠️  种子记录已丢失: {res['torrent_records_lost']} 项"
              f"（文件可还原，但需重新添加种子才能继续做种）")
    if res.get("unconfirmed"):
        print(f"  ❓ 当初未确认是否生效（unknown）: {res['unconfirmed']} 项——其中 "
              f"{res.get('unconfirmed_reverted', 0)} 项按此刻状态核对后已还原（计入上面的已还原）"
              + (f"，{res['unconfirmed_no_undo']} 项没有逆操作、需人工核对"
                 if res.get("unconfirmed_no_undo") else ""))
    if res.get("priority_not_restored"):
        print(f"  ⚠️  文件已搬回、合集条目的下载没恢复: {res['priority_not_restored']} 项"
              f"（它此刻没有种子做种）")
        for n in res.get("notes") or []:
            print(f"    ⚠️  {n}")
    for d in res["skipped_detail"]:
        tag = "（当初未确认）" if d.get("status") == "unknown" else ""
        print(f"    ⏭️  {tag}{d.get('skip_reason','')}")
    for d in res["failed_detail"]:
        tag = "（当初未确认）" if d.get("status") == "unknown" else ""
        print(f"    ❌ {tag}{d.get('error','')}")
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


def _stuck(cfg, run_id: str) -> list:
    """本轮之前连续 STUCK_RUNS 轮都在的问题（`history.find_stuck`），确认文件读不了就在 stderr 说一句。"""
    acks, problems = history.load_acks()
    for p in problems:
        _log(f"⚠️  确认文件：{p}")
    return history.find_stuck(cfg.state_dir, run_id, min_runs=cfg.stuck_runs, acks=acks)


def _print_stuck(cfg, stuck: list, limit: int = 20) -> None:
    """卡住的问题：同一个指纹连续 STUCK_RUNS 轮 run 都在。确认过的只计数（`.agents/acks.json`）。"""
    open_ = [s for s in stuck if not s.ack]
    acked = len(stuck) - len(open_)
    if not open_:
        if acked:
            print(f"\n═══ 卡住：没有未确认的（{acked} 个已确认、不再提醒，见 .agents/acks.json） ═══")
        return
    tail = f"（另有 {acked} 个已确认、不再提醒）" if acked else ""
    print(f"\n═══ 卡住：{len(open_)} 个问题连续 ≥{cfg.stuck_runs} 轮都在{tail} ═══")
    root = str(cfg.media_root).rstrip("/") + "/"
    for s in open_[:limit]:
        where = "" if s.target == s.show else f" {s.target.removeprefix(root)}"
        print(f"  ⏳ 连续 {s.runs} 轮（自 {s.first_seen}）[{s.rule}] {s.kind}【{s.show or '-'}】{where}")
        print(f"      {s.summary[:160]}")
        print(f"      指纹 {s.fp}——要人处理、先不提醒：media-agent ack {s.fp} --reason \"…\"")
    if len(open_) > limit:
        print(f"  …另 {len(open_) - limit} 个（media-agent health 看全部）")


_FP = re.compile(r"[0-9a-f]{6,16}")


def cmd_ack(args, cfg) -> int:
    """确认一个卡住的问题：写进 `.agents/acks.json`（版本化的用户意图，要提交入库）。"""
    path = history.acks_path()
    acks, problems = history.load_acks(path)
    if problems:
        # 读不了就不写：覆盖一个坏掉的文件会丢掉里面别的确认
        print(f"❌ {problems[0]}——先修好它再确认")
        return 2
    if args.list:
        if not acks:
            print("还没有任何确认（.agents/acks.json）")
        today = datetime.now().date()
        for fp, a in sorted(acks.items()):
            state = "有效" if history.active_ack(acks, fp, today) else "已过期"
            print(f"  {fp}  [{state}] {a.get('what', '')}")
            print(f"      {a.get('reason', '')}" + (f"（至 {a['until']}）" if a.get("until") else ""))
        return 0
    fp = (args.fingerprint or "").strip().lower()
    if not _FP.fullmatch(fp):
        print(f"❌ 指纹要写成 6–16 位十六进制（media-agent health / run 输出里的「指纹 …」），收到 {fp!r}")
        return 2
    if args.remove:
        hit = [k for k in acks if k.startswith(fp)]
        if len(hit) != 1:
            print(f"❌ .agents/acks.json 里{'没有' if not hit else '有不止一个'}以 {fp} 开头的确认")
            return 1
        acks.pop(hit[0])
        history.save_acks(acks, path)
        print(f"已撤销确认 {hit[0]}")
        _remind_commit(path)
        return 0
    if not (args.reason or "").strip():
        print("❌ 要写 --reason：为什么先不管它（半年后读到的人要看得懂）")
        return 2
    if args.until:
        try:
            datetime.strptime(args.until, "%Y-%m-%d")
        except ValueError:
            print(f"❌ --until 要写成 YYYY-MM-DD，收到 {args.until!r}")
            return 2
    seen = history.seen_fingerprints(cfg.state_dir)
    hits = [k for k in seen if k.startswith(fp)]
    if len(hits) > 1:
        print(f"❌ 以 {fp} 开头的指纹不止一个：{', '.join(sorted(hits)[:5])}——多写几位")
        return 2
    if hits:
        fp = hits[0]
        r = seen[fp]
        what = f"{r.get('rule')} / {r.get('kind')} / {r.get('target')}"
    elif args.force and len(fp) == 16:
        what = "（确认时发现历史里还没有）"
    else:
        print(f"❌ 最近的发现历史里没有指纹 {fp}（state/findings/）——打错了？"
              "要预先确认一个还没出现的问题，写全 16 位并加 --force")
        return 1
    entry = {"reason": args.reason.strip(), "added": datetime.now().date().isoformat(),
             "what": what}
    if args.until:
        entry["until"] = args.until
    acks[fp] = entry
    history.save_acks(acks, path)
    print(f"已确认 {fp}：{what}" + (f"，至 {args.until}" if args.until else ""))
    _remind_commit(path)
    return 0


def _remind_commit(path) -> None:
    print(f"\n⚠️  {path} 是版本化的用户意图，要提交入库：\n"
          f"    git add .agents/acks.json && git commit -m \"chore: 确认 …\"\n"
          "  在生产机上改的：下一次部署时漂移闸门会拦下它——用 deploy.sh --harvest 带回开发机提交、打 tag"
          "（deploy/README.md「用户意图放在哪」）")


def cmd_health(args, cfg) -> int:
    """最近一轮（或 `--run` 指定那一轮）的健康报告；`--json` 原样输出。

    `--accept-torrent-count`：把此刻 qBittorrent 的种子数认作新基线（人为批量删除之后）。"""
    if getattr(args, "accept_torrent_count", False):
        return _accept_torrent_count(cfg)
    rep = health.load_report(cfg.state_dir, getattr(args, "run", None))
    if rep is None:
        which = f"批次 {args.run} 的" if getattr(args, "run", None) else "任何"
        print(f"还没有{which}健康报告（state/health/，每轮 run 结束时写）")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    for line in health.render(rep, health.health_dir(cfg.state_dir) / f"{rep.get('run_id')}.json"):
        print(line)
    base = health.load_baseline(cfg.state_dir)
    if base:
        print(f"  种子数基线 {base['count']}（{base.get('source')}，批次 {base.get('run_id')}，"
              f"{base.get('ts')}）")
    return 0


def _accept_torrent_count(cfg) -> int:
    ctx = build_context(cfg)
    if ctx.qbit is None:
        return _refuse("qBittorrent 不可用：读不到此刻的种子数，基线不动")
    try:
        n = len(ctx.qbit.torrents())
    except Exception as e:                           # noqa: BLE001 —— 读不到就不动基线，照实说
        return _refuse(f"读不到种子列表（{type(e).__name__}: {e}），基线不动")
    prev = health.load_baseline(cfg.state_dir)
    problem = health.save_baseline(cfg.state_dir, count=n, run_id=new_run_id(),
                                   ts=datetime.now().isoformat(timespec="seconds"),
                                   source="accepted")
    if problem:
        print(f"❌ {problem}")
        return 1
    print(f"已把此刻的 {n} 个种子认作新的基线"
          + (f"（原来是 {prev['count']} 个，批次 {prev.get('run_id')}）" if prev else "")
          + "；下一轮 run 按它比")
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
        print("\n⚠️  反复失败 / 反复未确认的动作（可能是规则本身有问题）：")
        for f in failures:
            tag = "未确认" if f.get("status") == "unknown" else "失败"
            print(f"  {f['runs']} 个批次 / 共 {f['count']} 次 {tag} "
                  f"[{f['rule']}] {f['op']}: {f['error']}")
    return 0


def cmd_run(args, cfg) -> int:
    """一轮完整自治。收尾——正常结束、整批拒绝、异常冲出都一样——写健康报告（`health` 模块文档）。

    以前 `run` 只在"整批拒绝"与"审计没写全"时退出码非零；异常冲出就是一段 traceback，其余一切（抓取连着十天
    NameError、检测器崩了、磁盘快满）launchd 上都是 0。现在每轮都有一份 `state/health/<批次 ID>.json` 与输出末尾
    一小节，critical 时退出码非零（1 / 3 / 4 / 5，见 `health.exit_code_for`）。
    """
    # 批次 ID 先定下来：发现历史、审计、隔离区处置、健康报告、日志前缀都用这一个，彼此对得上
    # （经 `main` 起的由它先定好，日志前缀从拿锁之前就带上）
    rh = health.RunHealth(cfg, run_id=getattr(args, "run_id", None) or new_run_id(), cmd="run",
                          dry_run=args.dry_run or not cfg.auto_apply)
    try:
        rc = _run(args, cfg, rh)
    except Exception as e:
        # 冲出来的异常：完整 traceback 进 stderr（run.err.log），摘要进健康报告，退出码与未捕获异常同为 1
        rh.crashed(e)
        traceback.print_exc()
        rc = EXIT_CRASH
    except BaseException as e:
        # Ctrl-C / 被 kill 的 SystemExit：报告照写，异常照常往外抛
        rh.crashed(e)
        _finish_run(cfg, rh, EXIT_CRASH)
        raise
    return _finish_run(cfg, rh, rc)


def _finish_run(cfg, rh, rc: int) -> int:
    """定状态、写报告、打印一小节；返回最终退出码。这一步自己出错不改变这一轮的退出码。"""
    try:
        # 报告里可能混进带凭据的报错（httpx 的异常会带上整个请求 URL，TMDB 的 api_key 就在里面）
        rep = notify.redact_obj(cfg, rh.finish(rc))
        # 有变化才发通知，一轮最多一封；发不出去只在 stderr 说、记进报告，不改退出码
        rep["notify"] = notify.maybe_send(cfg, rep)
        path, problems = health.write_report(cfg.state_dir, rep)
        for line in health.render(rep, path):
            print(line)
        for p in problems:
            _log(f"⚠️  健康报告：{p}")
        return rep["exit_code"]
    except Exception as e:                           # noqa: BLE001 —— 观测出错不能吞掉这一轮的结论
        _log(f"⚠️  健康报告出错（{type(e).__name__}: {e}），本轮退出码按原样 {rc}")
        traceback.print_exc()
        return rc


def _run(args, cfg, rh) -> int:
    run_id = rh.data["run_id"]
    ctx = build_context(cfg, need_llm=True)
    ctx.log = rh.tap(ctx.log)
    rh.clients(ctx)
    prev = health.load_baseline(cfg.state_dir)
    scanned_at = datetime.now().isoformat(timespec="seconds")
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    rh.scanned(state, prev)
    if not state.qbit_errors:
        # 这一轮的种子数被采信了：下一轮拿它比（`health.torrent_count_problem`）。时间取扫描之前——
        # 这一轮自己摘掉的种子也算进下一轮"解释得通"的那部分。被拒绝的一轮不挪基线。
        problem = health.save_baseline(cfg.state_dir, count=len(state.torrents), run_id=run_id,
                                       ts=scanned_at, source="run")
        if problem:
            _log(f"⚠️  {problem}")
    reg = build_registry()

    findings = reg.run_all(ctx, state)
    rh.diagnosed(reg, findings)
    _record_findings(cfg, run_id, findings, state, "run")
    print(f"═══ 诊断：{len(findings)} 个问题 ═══")
    _warn_degraded(state)
    _print_findings(findings, False)

    dry = args.dry_run or not cfg.auto_apply
    ex = Executor(ctx, dry_run=dry, run_id=run_id)
    report = ex.apply(findings)
    if report.refused:
        # fail closed：不修、不演进（演进器会拿这份残缺快照去立规则）、
        # 也不处置隔离区——降级的一轮不改动任何东西。
        rh.refused(report.refused)
        return _refuse(report.refused)
    rh.applied(report, findings, state)
    print(f"\n═══ 修复：{report.summary()} ═══")
    _print_unknown(report.unknown)

    rc = 0
    if cfg.evolve_mode != "propose":
        # 冻结：不重扫、不调 LLM、不构造 Evolver（它的 __init__ 就会建 .agents/rules）
        rh.evolve("frozen")
        print(f"\n═══ 演进：已冻结（EVOLVE_MODE={cfg.evolve_mode}） ═══")
    elif ctx.llm.enabled and not args.no_evolve:
        # 修复后重新扫描，残留才是真盲区
        state2 = build_state(ctx, resolve_tmdb=False)
        if state2.qbit_errors:
            # 种子视图残缺时，有种子的文件全变成"无主文件"，演进器会拿它们当
            # 盲区去立规则——规则一旦上线就是永久的。宁可这轮不演进。
            rh.rescan_degraded(state2.qbit_errors[0])
            rh.evolve("skipped")
            print(f"\n═══ 演进：跳过——重扫时 qBittorrent 数据不完整："
                  f"{state2.qbit_errors[0]} ═══")
            rc = EXIT_DEGRADED
        else:
            findings2 = reg.run_all(ctx, state2)
            results = Evolver(ctx, reg).evolve(state2, findings2,
                                               max_proposals=args.max_proposals)
            promoted = [r for r in results if r["outcome"] == "promoted"]
            rh.evolve(f"proposed {len(results)}, promoted {len(promoted)}")
            print(f"\n═══ 演进：提议 {len(results)} 条，上线 {len(promoted)} 条 ═══")
            for r in promoted:
                print(f"  🎉 {r['rule_id']}")
    else:
        rh.evolve("off")

    # 隔离区处置（disposal 模块文档）：以前这里按日期 rmtree 整个日目录、一行记录都不写
    # （Executor.purge_trash，生产上删掉过 6 个文件 4.7GB，说不出是哪几个）。现在按处置类别
    # 逐个判、逐个预写日志后删；要人定的过了保留期只报不删。
    rep = disposal.dispose(ctx, mode="run", run_id=ex.run_id, dry_run=dry)
    rh.disposed(rep)
    _print_disposal(cfg, rep)
    stuck = _stuck(cfg, run_id)
    rh.stuck(stuck)
    _print_stuck(cfg, stuck)
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

    s = sub.add_parser("ack", help="确认一个卡住的问题：先不提醒（写 .agents/acks.json，要提交入库）")
    s.add_argument("fingerprint", nargs="?", help="指纹（run / health 输出里的「指纹 …」，可写前 6 位以上）")
    s.add_argument("--reason", help="为什么先不管它")
    s.add_argument("--until", help="到哪天（YYYY-MM-DD，含当天）为止，过了重新提醒")
    s.add_argument("--remove", action="store_true", help="撤销这个确认")
    s.add_argument("--force", action="store_true", help="发现历史里还没有这个指纹也确认（要写全 16 位）")
    s.add_argument("--list", action="store_true", help="列出全部确认")
    s.set_defaults(func=cmd_ack)

    s = sub.add_parser("health", help="最近一轮（或指定一轮）的健康报告")
    s.add_argument("--run", help="批次 ID，省略则看最近一轮")
    s.add_argument("--json", action="store_true", help="原样输出 JSON")
    s.add_argument("--accept-torrent-count", action="store_true",
                   help="把此刻 qBittorrent 的种子数认作新基线（在 qBit 里手动批量删除之后）")
    s.set_defaults(func=cmd_health)

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
    if args.cmd == "run":
        # launchd 把这些输出追加进 state/run.log / run.err.log：每一行带时间与批次 ID（`runlog`）
        args.run_id = new_run_id()
        with runlog.stamped(args.run_id):
            return _locked(args, cfg, before=_run_banner)
    return _locked(args, cfg)


def _run_banner(args, cfg) -> None:
    """`run` 拿到锁之后、做任何事之前：轮转日志（锁保证没有别的 media-agent 同时在写），打一行轮次分隔。"""
    rotated = runlog.rotate(cfg.state_dir)
    print(f"═══ media-agent {__version__} run 开始：批次 {args.run_id}"
          f"{'（预演）' if args.dry_run or not cfg.auto_apply else ''} ═══")
    for m in rotated:
        _log(m)


def _locked(args, cfg, before=None) -> int:
    """需要运行锁的子命令先拿锁（见 runlock.py），拿不到以 75 结束。`before` 在拿到锁之后、执行之前调用。"""
    if not _needs_lock(args):
        if before:
            before(args, cfg)
        return args.func(args, cfg)
    lock = RunLock(cfg.state_dir / LOCK_NAME, label=" ".join(["media-agent", *sys.argv[1:]]))
    if not lock.acquire(wait=runlock.DEFAULT_WAIT):
        return _locked_out(lock.holder())
    try:
        if before:
            before(args, cfg)
        return args.func(args, cfg)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
