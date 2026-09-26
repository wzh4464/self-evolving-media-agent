"""EVOLVE_MODE：自演进默认冻结。

evolution 调研（2026-09-26）：2026-08-20 之后 `run` 的演进步连续 147 轮「提议 0 条」，
生产 30 条演进规则全无动作、今天零命中——关掉它行为中立。而它会往仓库目录
`.agents/rules/`、`.agents/notes/` 写文件：改成按 git tag 部署后，这些未入库的文件
会让部署的漂移闸门拒绝部署（critic §3.8）。所以默认 `off`：不重扫、不调 LLM、
不构造 Evolver（它的 `__init__` 就会建 rules 目录）；`propose` 保留旧行为。
"""
from __future__ import annotations

import argparse

import pytest

from media_agent import cli
from media_agent import config as config_mod
from media_agent import evolution as evolution_mod
from media_agent.config import load_config
from media_agent.evolution import Residue


def _args(**kw):
    base = dict(dry_run=False, no_tmdb=True, no_evolve=False, max_proposals=3)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def offline_cli(lib, monkeypatch):
    monkeypatch.setattr(cli, "build_context", lambda cfg, need_llm=False: lib.context())
    s1 = lib.show("测试番").season(1)
    s1.single("测试番 S01E01.mkv", name="[G] Test - 01.mkv")
    # 造一簇"盲区"：演进分支一旦走到，就会拿它去问 LLM
    monkeypatch.setattr(evolution_mod, "find_residue", lambda state, findings: [
        Residue("[…] 测试番 - #.mkv", samples=[{"filename": "[G] 测试番 - 02.mkv"}], count=3)])
    lib.llm.script({"worth_a_rule": False})        # 打开 FakeLLM，记下每次调用
    return lib


def _count_scans(monkeypatch) -> dict:
    n = {"scans": 0}
    real = cli.build_state

    def counting(ctx, **kw):
        n["scans"] += 1
        return real(ctx, **kw)

    monkeypatch.setattr(cli, "build_state", counting)
    return n


def test_evolve_mode_defaults_to_off(monkeypatch):
    assert load_config().evolve_mode == "off"
    monkeypatch.setenv("EVOLVE_MODE", " Propose ")
    assert load_config().evolve_mode == "propose"


def test_evolve_mode_typo_fails_loudly(monkeypatch, capsys):
    """写错不能静默当成 off（以为开着）或 propose（往仓库里写文件）。"""
    monkeypatch.setenv("EVOLVE_MODE", "on")
    with pytest.raises(ValueError, match="EVOLVE_MODE"):
        load_config()
    monkeypatch.setattr("sys.argv", ["media-agent", "runs"])
    assert cli.main() == 2
    assert "配置错误" in capsys.readouterr().err


def test_run_with_evolve_off_touches_neither_llm_nor_agents_dir(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    assert lib.cfg.evolve_mode == "off"                  # 默认值，未显式设置
    scans = _count_scans(monkeypatch)
    monkeypatch.setattr(evolution_mod.Evolver, "__init__",
                        lambda *a, **k: pytest.fail("冻结时不该构造 Evolver"))

    rc = cli.cmd_run(_args(), lib.cfg)

    assert rc == 0
    assert lib.llm.prompts == []                         # 一次 LLM 调用都没有
    assert scans["scans"] == 1                           # 没有为演进重扫
    assert not evolution_mod.RULES_DIR.exists()          # .agents/rules 都没建
    assert not evolution_mod.NOTES_ROOT.exists()
    assert "演进：已冻结（EVOLVE_MODE=off）" in capsys.readouterr().out


def test_run_with_evolve_propose_keeps_the_old_behaviour(offline_cli, monkeypatch, capsys):
    lib = offline_cli
    lib.configure(evolve_mode="propose")
    scans = _count_scans(monkeypatch)

    rc = cli.cmd_run(_args(), lib.cfg)

    assert rc == 0
    assert len(lib.llm.prompts) == 1                     # 拿盲区去问了模型
    assert scans["scans"] == 2                           # 修复后为演进重扫一次
    assert "演进：提议 1 条，上线 0 条" in capsys.readouterr().out


def test_no_evolve_flag_still_wins_in_propose_mode(offline_cli, monkeypatch):
    lib = offline_cli
    lib.configure(evolve_mode="propose")
    scans = _count_scans(monkeypatch)

    assert cli.cmd_run(_args(no_evolve=True), lib.cfg) == 0

    assert lib.llm.prompts == [] and scans["scans"] == 1


def test_manual_evolve_refuses_while_frozen(offline_cli, capsys):
    lib = offline_cli

    assert cli.cmd_evolve(_args(), lib.cfg) == 1

    assert lib.llm.prompts == []
    assert not evolution_mod.RULES_DIR.exists()
    assert "EVOLVE_MODE=propose media-agent evolve" in capsys.readouterr().out


def test_config_default_is_off_for_directly_built_configs():
    """测试基座与其它直接构造 Config 的地方不传这个字段，也得是冻结。"""
    assert config_mod.Config.__dataclass_fields__["evolve_mode"].default == "off"
