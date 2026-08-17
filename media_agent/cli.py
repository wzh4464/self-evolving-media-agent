"""命令行入口。

    media-agent scan      # 只扫描，看看库里现在什么样
    media-agent diagnose  # 跑全部规则，出问题清单（不改动任何东西）
    media-agent apply     # 执行修复（--dry-run 预演）
    media-agent evolve    # 找规则盲区 → 提议新规则 → 验证 → 提升
    media-agent run       # 一轮完整自治：diagnose → apply → evolve → 清理隔离区
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .actions import Executor
from .cache import Cache
from .clients import (
    AniListClient, AutoBangumiClient, AutoBangumiDB, LLMClient, QBitClient, TMDBClient,
)
from .config import load_config
from .evolution import Evolver, find_failure_patterns, find_residue, load_evolved
from .kernel import Context, Registry
from .plugins import register_builtins
from .scan import build_state


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


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

    dry = args.dry_run or not cfg.auto_apply
    ex = Executor(ctx, dry_run=dry)
    report = ex.apply(findings)

    print(("【预演】" if dry else "【已执行】") + report.summary())
    for rec in report.applied:
        print(f"  ✅ [{rec['op']}] {rec['summary']}")
    for rec in report.skipped:
        print(f"  ⏭️  [{rec['op']}] {rec['summary']} —— {rec.get('reason','')}")
    for rec in report.failed:
        print(f"  ❌ [{rec['op']}] {rec['summary']} —— {rec.get('error','')}")
    return 0


def cmd_evolve(args, cfg) -> int:
    ctx = build_context(cfg, need_llm=True)
    if not ctx.llm.enabled:
        print("未配置 LLM_KEY，无法演进")
        return 1

    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
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
            print(f"  ×{f['count']} [{f['rule']}] {f['op']}: {f['error']}")
    return 0


def cmd_run(args, cfg) -> int:
    """一轮完整自治。"""
    ctx = build_context(cfg, need_llm=True)
    state = build_state(ctx, resolve_tmdb=not args.no_tmdb)
    reg = build_registry()

    findings = reg.run_all(ctx, state)
    print(f"═══ 诊断：{len(findings)} 个问题 ═══")
    _print_findings(findings, False)

    dry = args.dry_run or not cfg.auto_apply
    ex = Executor(ctx, dry_run=dry)
    report = ex.apply(findings)
    print(f"\n═══ 修复：{report.summary()} ═══")

    if ctx.llm.enabled and not args.no_evolve:
        # 修复后重新扫描，残留才是真盲区
        state2 = build_state(ctx, resolve_tmdb=False)
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
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="media-agent", description="番剧媒体库自治 agent")
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
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("evolve", help="自演进：为规则盲区提议新规则")
    s.add_argument("--max-proposals", type=int, default=3)
    s.set_defaults(func=cmd_evolve)

    s = sub.add_parser("run", help="完整自治轮次")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-evolve", action="store_true")
    s.add_argument("--max-proposals", type=int, default=3)
    s.set_defaults(func=cmd_run)

    args = p.parse_args()
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
