"""`media-agent run` 迭代到不动点（`converge.run`）：一轮完成以前要两三轮的事，发现历史 / 卡住检测 / 健康报告按
**最后一次**诊断算、各次迭代的合计另列，到顶与两条规则打架在健康报告里是 warn，任何一次迭代读不全都按原来的拒绝语义收尾。

`cmd_run` 原样跑（`build_context` 换成基座的 Context），断言落在磁盘、审计、`state/health/`、`state/findings/` 上。
"""
from __future__ import annotations

import argparse

import pytest

from harness import video, weekly
from media_agent import cli, converge, health, history, titles
from media_agent.config import load_config
from media_agent.kernel import Action, Finding

LOLI_08 = "[LoliHouse] Yani Neko - 08 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_08 = "[Dynamis One] Yani Neko - 08 (ABEMA 1920x1080 AVC AAC MKV).mkv"
TWO_SUBS = video("hevc", subs=["chi 简体中文", "chi 繁體中文"])
SHOW = "尼古喵喵"


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    lib.tmdb.enabled = True             # 与生产一致：配了 TMDB（没配是一条 warn）
    return lib


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=0, json=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _latest(lib) -> dict:
    return health.load_report(lib.cfg.state_dir)


def _ab_duplicate(lib):
    s1 = lib.show(SHOW).season(1)
    raw = s1.single(f"{SHOW} S01E08.mkv", size=745_065_995, name=ABEMA_08, probe=video("h264"))
    sub = s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS, category="Bangumi")
    return s1, raw, sub


class _Chain:
    """每次迭代都有一件新事：`x1.mkv` → `x2.mkv` → …（本地文件）。"""
    id = "chain"

    def detect(self, ctx, state):
        for show in state.shows:
            for f in show.files:
                if f.filename.startswith("x") and f.filename[1:-4].isdigit():
                    n = int(f.filename[1:-4])
                    yield Finding(rule=self.id, kind="chain", severity="minor", summary=f.filename,
                                  show=show.dir_name, path=str(f.path),
                                  action=Action(op="rename", args={
                                      "path": str(f.path), "new_name": f"x{n + 1}.mkv", "torrent_hash": ""}))


class _Flip:
    def __init__(self, rule: str, src: str, dst: str):
        self.id, self.src, self.dst = rule, src, dst

    def detect(self, ctx, state):
        for show in state.shows:
            for f in show.files:
                if f.filename == self.src:
                    yield Finding(rule=self.id, kind="flip", severity="minor",
                                  summary=f"{self.src} → {self.dst}", show=show.dir_name,
                                  path=str(f.path), torrent_hash=f.torrent_hash,
                                  action=Action(op="rename", args={
                                      "path": str(f.path), "new_name": self.dst,
                                      "torrent_hash": f.torrent_hash}))


# ------------------------------------------------------------------ 一轮完成
def test_run_finishes_an_ab_duplicate_in_one_run(offline_cli, capsys):
    lib = offline_cli
    s1, raw, sub = _ab_duplicate(lib)
    sub_ident = lib.ident(sub.path)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = _latest(lib)
    assert lib.disk() == {f"{SHOW}/Season 1/{SHOW} S01E08.mkv": 593_601_176}
    assert lib.ident(s1.path / f"{SHOW} S01E08.mkv") == sub_ident
    assert rep["status"] == "ok", rep["reasons"]
    loop = rep["loop"]
    assert loop["max"] == 3 and loop["stop"] == converge.FIXED_POINT
    assert [it["applied"] for it in loop["iterations"]] == [3, 2, 0]
    applied = [r["op"] for r in lib.audit(rep["run_id"]) if r["status"] == "applied"]
    assert rep["actions"]["applied"] == len(applied) == 5            # 各次迭代合计
    assert {r["run_id"] for r in lib.audit()} == {rep["run_id"]}      # 一个批次
    out = capsys.readouterr().out
    assert "迭代 1/3" in out and "迭代 2/3" in out and "迭代 3/3" in out
    assert "3 次迭代，不动点" in out


def test_findings_history_is_written_once_from_the_final_diagnosis(offline_cli, capsys):
    """发现历史、卡住检测按一轮一份、按这一轮结束时的样子算：已经在这一轮修好的不算"还在"。"""
    lib = offline_cli
    _ab_duplicate(lib)

    cli.cmd_run(_args(), lib.cfg)

    [snap] = history.load_snapshots(lib.cfg.state_dir)
    kinds = {r["kind"] for r in snap.findings}
    assert not kinds & {"duplicate", "unrenamed", "pending_ownership", "category_fragmented"}, kinds
    rep = _latest(lib)
    assert rep["findings"]["total"] == len(snap.findings)
    assert rep["findings"]["actionable"] == 0


def test_a_run_that_iterates_still_counts_as_one_run_for_the_title_gate(offline_cli):
    """TMDB 标题稳定闸数的是 `run`（连续两轮才采用新标题）。迭代之间记一次，同一轮里第二次迭代就会把新标题
    "确认"下来——一次 TMDB 抖动就改名，LAT-04 又回来了。"""
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(1, "药屋少女的独语", seasons={1: weekly(4, first_days_ago=900)})
    sh = lib.show("药屋少女的呢喃")
    s1 = sh.season(1)
    for n in (1, 2, 3):
        s1.local(f"药屋少女的呢喃 S01E{n:02d}.mkv")
    s1.single("[G] Kusuriya - 04 [1080p].mkv")                          # 要改名：让这一轮迭代两次以上
    sh.sidecar(tmdb_id=1, tmdb_title="药屋少女的呢喃")

    assert cli.cmd_run(_args(no_tmdb=False), lib.cfg) == 0

    rep = _latest(lib)
    assert len([it for it in rep["loop"]["iterations"] if not it["final"]]) >= 2
    book, problem = titles.load(lib.cfg.state_dir)
    assert problem == "" and book.seq == 1
    assert book.entry(1)["pending"]["runs"] == 1
    assert sorted(lib.disk()) == [f"药屋少女的呢喃/Season 1/药屋少女的呢喃 S01E0{n}.mkv" for n in (1, 2, 3, 4)]


# ------------------------------------------------------------------ (e) 第二次迭代读不全
@pytest.mark.allow("log_failure", match="qBittorrent 数据不完整")
def test_a_degraded_second_iteration_refuses_like_a_degraded_first_one(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    _s1, _raw, sub = _ab_duplicate(lib)
    real = cli.build_state
    scans = {"n": 0}

    def scan(ctx, **kw):
        scans["n"] += 1
        if scans["n"] == 2:
            lib.qbit.fail("files", hash=sub.hash, times=None)
        return real(ctx, **kw)

    monkeypatch.setattr(cli, "build_state", scan)
    disposed = []
    monkeypatch.setattr(cli.disposal, "dispose", lambda *a, **k: disposed.append(1))

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_DEGRADED == rep["exit_code"]
    assert rep["status"] == "critical" and [r["code"] for r in rep["reasons"]] == ["refused"]
    assert rep["loop"]["stop"] == converge.REFUSED and len(rep["loop"]["iterations"]) == 2
    assert "第 2 次迭代" in rep["degraded"]["refused"]
    assert rep["degraded"]["qbit_errors"]                             # 读不全的是第二次扫描
    assert rep["actions"]["applied"] == 3                             # 第一次迭代做的照样报
    assert disposed == [] and scans["n"] == 2                          # 不处置隔离区、不再扫描
    [snap] = history.load_snapshots(lib.cfg.state_dir)
    assert snap.degraded
    out = capsys.readouterr().out
    assert "⛔ 拒绝执行任何改动：第 2 次迭代" in out


# ------------------------------------------------------------------ (f) 到顶
def test_reaching_the_cap_is_a_warning_listing_what_is_left(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    lib.configure(max_iterations=2)
    s1 = lib.show(SHOW).season(1)
    s1.single(f"{SHOW} S01E01.mkv")
    s1.local("x1.mkv")
    monkeypatch.setattr(cli, "build_registry", lambda: lib.registry([_Chain()]))

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == 0 and rep["status"] == "warn"
    [r] = [r for r in rep["reasons"] if r["code"] == "loop_cap"]
    assert r["level"] == "warn" and "x3.mkv" in r["text"] and "2 次" in r["text"]
    assert rep["loop"]["stop"] == converge.CAP
    assert rep["loop"]["pending_count"] == 1 and rep["loop"]["pending"][0]["op"] == "rename"
    out = capsys.readouterr().out
    assert "迭代到上限 2 次仍有 1 个动作待做" in out
    assert "迭代    2 次 · 到上限" in out                                  # 健康一节


@pytest.mark.allow("log_failure", match="数据不完整")
def test_a_degraded_cap_pass_is_a_critical_rescan_with_exit_3(offline_cli, monkeypatch, capsys):
    """到顶之后的收尾诊断读 qBittorrent 不完整：修复已经做了、处置照跑，但这一轮最后看到的是残缺的视图——与演进重扫
    读不全同一个口径：`degraded.rescan`、critical、退出码 3（2026-09-27 审查：这一支没有测试，把退出码改成 0、或
    不记 `rescan_degraded` 的变异全套存活）。"""
    lib = offline_cli
    _degraded_cap_pass(lib, monkeypatch)

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_DEGRADED == rep["exit_code"]
    assert rep["status"] == "critical" and rep["degraded"]["rescan"]
    assert rep["loop"]["stop"] == converge.CAP and rep["loop"]["final_degraded"]
    assert rep["loop"]["pending_count"] == 0                           # 残缺的视图里列不出待做
    assert rep["actions"]["applied"] == 2                              # 修复做了
    assert rep["trash"] and rep["trash"]["disposal"] is not None       # 处置照跑
    assert "收尾诊断：qBittorrent 数据不完整" in capsys.readouterr().out


def _degraded_cap_pass(lib, monkeypatch) -> None:
    """两次迭代到顶（`_Chain`），之后的收尾诊断读某个种子的文件列表失败。"""
    lib.configure(max_iterations=2)
    s1 = lib.show(SHOW).season(1)
    t = s1.single(f"{SHOW} S01E01.mkv")
    s1.local("x1.mkv")
    monkeypatch.setattr(cli, "build_registry", lambda: lib.registry([_Chain()]))
    real, scans = cli.build_state, []

    def scan(ctx, **kw):
        scans.append(1)
        if len(scans) == 3:                                     # 两次迭代之后的收尾诊断
            lib.qbit.fail("files", hash=t.hash, times=None)
        return real(ctx, **kw)

    monkeypatch.setattr(cli, "build_state", scan)


@pytest.mark.allow("log_failure", match="数据不完整")
def test_a_degraded_cap_pass_still_exits_3_when_the_health_report_breaks(offline_cli, monkeypatch):
    """健康报告自己出错时退出码按 `_run` 给的原样返回（`_finish_run`）：那时只剩它说"这一轮最后看到的是残缺的视图"。
    平时退出码由健康报告的 critical 原因定（`health.exit_code_for`），所以只有这条路能看出 `_run` 给的对不对。"""
    lib = offline_cli
    _degraded_cap_pass(lib, monkeypatch)

    def broken(*a, **k):
        raise OSError("state/health 写不进去")

    monkeypatch.setattr(cli.health, "write_report", broken)

    assert cli.cmd_run(_args(), lib.cfg) == cli.EXIT_DEGRADED


def test_two_rules_fighting_is_a_warning_and_a_finding(offline_cli, monkeypatch):
    lib = offline_cli
    s1 = lib.show(SHOW).season(1)
    s1.single("a.mkv")
    monkeypatch.setattr(cli, "build_registry",
                        lambda: lib.registry([_Flip("rule-a", "a.mkv", "b.mkv"),
                                              _Flip("rule-b", "b.mkv", "a.mkv")]))

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = _latest(lib)
    [r] = [r for r in rep["reasons"] if r["code"] == "oscillation"]
    assert "rule-b" in r["text"] and "rule-a" in r["text"]
    [snap] = history.load_snapshots(lib.cfg.state_dir)
    assert [f["rule"] for f in snap.findings if f["kind"] == "oscillation"] == ["run-loop"]


def test_max_iterations_1_is_the_old_single_pass(offline_cli):
    lib = offline_cli
    lib.configure(max_iterations=1)
    _ab_duplicate(lib)

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = _latest(lib)
    assert [it["applied"] for it in rep["loop"]["iterations"] if not it["final"]] == [3]
    assert rep["loop"]["stop"] == converge.CAP
    assert sorted(p["op"] for p in rep["loop"]["pending"]) == ["rename", "trash"]


def test_dry_run_reports_one_iteration(offline_cli):
    lib = offline_cli
    _ab_duplicate(lib)

    assert cli.cmd_run(_args(dry_run=True), lib.cfg) == 0

    rep = _latest(lib)
    assert rep["loop"]["stop"] == converge.DRY_RUN and len(rep["loop"]["iterations"]) == 1


# ------------------------------------------------------------------ 配置
def test_max_iterations_defaults_to_3_and_env_overrides(monkeypatch):
    assert load_config().max_iterations == 3
    monkeypatch.setenv("MAX_ITERATIONS", "1")
    assert load_config().max_iterations == 1


@pytest.mark.parametrize("bad", ["0", "-1", "x", ""])
def test_bad_max_iterations_fails_loudly(monkeypatch, bad):
    monkeypatch.setenv("MAX_ITERATIONS", bad)
    with pytest.raises(ValueError, match="MAX_ITERATIONS"):
        load_config()


# ------------------------------------------------------------------ 更多"以前要两轮"的现场
LOLI_09 = "[LoliHouse] Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv"
ABEMA_09 = "[Dynamis One] Yani Neko - 09 (ABEMA 1920x1080 AVC AAC MKV).mkv"


def test_run_grabs_and_renames_the_grab_in_one_run(offline_cli):
    """(b) 抓取之后元数据没在等待时限内到：以前"下一轮会补"，文件在发布名上躺一轮（2026-09-03 最新三集刮削失败）。"""
    from harness import MikanItem
    lib = offline_cli
    lib.configure(qbit_allow_empty=True, grab_metadata_timeout=0)
    sh = lib.show(SHOW)
    s1 = sh.season(1)
    for n in range(1, 9):
        s1.local(f"{SHOW} S01E{n:02d}.mkv")
    schedule = weekly(12, first_days_ago=60)
    sh.tmdb(1234, seasons={1: schedule})
    sh.sidecar(tmdb_id=1234, tmdb_title=SHOW, mikan_id="3500", seasons={"1": {"have": list(range(1, 9))}})
    title = "[LoliHouse] 尼古喵喵 / Yani Neko - 09 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
    item = MikanItem(title=title, pub=dict(schedule)[9])
    lib.mikan("3500", [item], search=[SHOW])
    lib.qbit.fail("files", hash=item.infohash, times=1)               # 加种后第一次读文件列表：元数据还没到

    assert cli.cmd_run(_args(no_tmdb=False), lib.cfg) == 0

    rep = _latest(lib)
    recs = {r["op"]: r for r in lib.audit(rep["run_id"]) if r["status"] == "applied"}
    assert recs["grab_episode"]["metadata"]["outcome"] == "timeout"
    assert recs["rename"]["args"]["torrent_hash"] == item.infohash
    assert lib.qbit.file_names(item.infohash) == [f"{SHOW} S01E09.mkv"]
    assert rep["grab"]["proposed"] == 1 and rep["grab"]["applied"] == 1
    assert rep["unrenamed"]["count"] == 0 and rep["status"] == "ok", rep["reasons"]


def test_run_delete_quota_covers_the_whole_run(offline_cli):
    """(d) 删除配额管整轮：第二次迭代才轮到的判重被「已达单轮删除数量上限」拦下、不再重试，健康报告照实说。"""
    lib = offline_cli
    lib.configure(max_delete_per_run=1)
    s1 = lib.show(SHOW).season(1)
    s1.single(f"{SHOW} S01E08.mkv", size=745_065_995, name=ABEMA_08, probe=video("h264"))
    s1.single(LOLI_08, size=593_601_176, probe=TWO_SUBS)
    raw9 = s1.single(f"{SHOW} S01E09.mkv", size=745_065_996, name=ABEMA_09, probe=video("h264"))
    s1.single(LOLI_09, size=593_601_177, probe=TWO_SUBS, category="Bangumi")

    assert cli.cmd_run(_args(), lib.cfg) == 0

    rep = _latest(lib)
    trashes = [r for r in lib.audit(rep["run_id"]) if r["op"] == "trash"]
    assert [r["status"] for r in trashes] == ["applied", "skipped"]
    assert trashes[1]["reason"] == "已达单轮删除数量上限 1"
    assert lib.qbit.has(raw9.hash) and len(lib.trash_files()) == 1
    assert rep["loop"]["stop"] == converge.FIXED_POINT


def test_run_renames_a_show_dir_and_writes_its_sidecar_in_one_run(offline_cli):
    """目录改名（op 8）排在写档案（op 10）前面：旧目录没了，档案"下一轮按新目录重算"。现在下一次迭代就按新目录写。"""
    lib = offline_cli
    lib.configure(qbit_allow_empty=True)
    lib.tmdb.add_show(77, "新名字", seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("旧名字")
    for n in (1, 2):
        sh.season(1).local(f"新名字 S01E{n:02d}.mkv")
    sh.sidecar(tmdb_id=77, tmdb_title="新名字")

    assert cli.cmd_run(_args(no_tmdb=False), lib.cfg) == 0

    rep = _latest(lib)
    ops = [(r["op"], r["status"]) for r in lib.audit(rep["run_id"])]
    assert ("rename_show_dir", "applied") in ops
    sidecars = [r for r in lib.audit(rep["run_id"]) if r["op"] == "write_sidecar"]
    assert [(r["status"], r["args"]["show_dir"].rsplit("/", 1)[-1]) for r in sidecars] == \
        [("skipped", "旧名字"), ("applied", "新名字")]                  # 第一次迭代：目录已不在；第二次：按新目录写
    assert not (lib.media_root / "旧名字").exists()
    assert lib.sidecar("新名字").seasons["1"]["have"] == [1, 2]
    assert rep["status"] == "ok", rep["reasons"]


def test_run_stops_while_qbittorrent_is_still_moving_and_says_so(offline_cli, monkeypatch, capsys):
    """目录改名之后 qBittorrent 在时限内没搬完：这一轮停在第一次迭代（不在半搬的视图上诊断），退出码 0，输出与健康
    报告写明为什么停、剩下的下一轮做。"""
    lib = offline_cli
    lib.qbit.async_moves = True
    monkeypatch.setattr(converge, "SETTLE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(converge, "_sleep", lambda s: None)
    lib.tmdb.add_show(77, "新名字", seasons={1: weekly(2, first_days_ago=900)})
    sh = lib.show("旧名字")
    sh.season(1).torrent({"新名字 S01E01.mkv": 600_000_000, "新名字 S01E02.mkv": 600_000_000},
                         name="新名字 01-02", layout="nosub")
    sh.sidecar(tmdb_id=77, tmdb_title="新名字")

    assert cli.cmd_run(_args(no_tmdb=False), lib.cfg) == 0

    rep = _latest(lib)
    assert rep["loop"]["stop"] == converge.MOVING and "还在搬" in rep["loop"]["unsettled"]
    assert len(rep["loop"]["iterations"]) == 1
    out = capsys.readouterr().out
    assert "qBittorrent 还在搬存储" in out and "剩下的下一轮做" in out
    assert any("qBittorrent 还在搬存储" in line for line in health.render(rep))


# ------------------------------------------------------------------ 半路冲出
def test_a_crash_in_a_later_iteration_still_reports_what_earlier_iterations_did(offline_cli, monkeypatch,
                                                                                capsys):
    """第二次迭代的扫描抛了异常：第一次迭代已经改了东西（审计里有）。健康报告不能把"执行"一节写成 null——那是
    "没跑到"，而它跑了。"""
    lib = offline_cli
    _ab_duplicate(lib)
    real, n = cli.build_state, []

    def scan(ctx, **kw):
        n.append(1)
        if len(n) == 2:
            raise OSError("媒体卷掉线了")
        return real(ctx, **kw)

    monkeypatch.setattr(cli, "build_state", scan)

    rc = cli.cmd_run(_args(), lib.cfg)

    rep = _latest(lib)
    assert rc == cli.EXIT_CRASH and rep["reasons"][0]["code"] == "crash"
    assert rep["actions"]["applied"] == 3
    assert [it["applied"] for it in rep["loop"]["iterations"]] == [3]
    assert rep["loop"]["stop"] == converge.CRASHED
    out, err = capsys.readouterr()
    assert "Traceback" in err and "迭代    1 次 · 半路冲出" in out
