"""被吞掉的异常：每一个 `except Exception` / 裸 `except` 要么说出来（日志 / 审计 / 往上抛 / 记进错误列表），
要么在那一行写明为什么不说是对的。

这个项目到处都在吞异常，而且吞得有理由——单条规则、单个请求出错不能拖垮整轮。可吞得不声不响的那几处，
正是"代码在跑、日志正常，只是不再做它该做的事"的来源（`bug-fix/2026-08-30-silent-failures-sweep.md`：
五个"不报错、只是不干活"的 bug；critic N9：检测器里一个被吞掉的 "database is locked"）。第 3 阶段逐个过了一遍，
这个测试防止以后再加一个不声不响的。

判定（按 AST）：处理块里调用了下面这些之一就算"说出来了"——`log` / `_log` / `print` / `_stderr`（日志）、
`_audit` / `_settle` / `_settle_move` / `_crashed`（审计）、`on_error` / `errors.append` / `failed.append` /
`results.append`（交给调用方汇报）、`_refuse` / `crashed`，或者 `raise`，或者把异常本身交出去（`return f"…{e}"`）；
否则 `except` 那一行必须带注释，写明为什么不说。
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "media_agent"

_REPORTING_CALLS = {"log", "_log", "print", "_stderr", "_audit", "_settle", "_settle_move",
                    "_crashed", "on_error", "_refuse", "crashed", "print_exc"}
_REPORTING_APPENDS = {"errors", "failed", "results", "problems", "load_errors"}


def _broad(h: ast.ExceptHandler) -> bool:
    if h.type is None:
        return True
    names = [h.type] if not isinstance(h.type, ast.Tuple) else list(h.type.elts)
    return any(isinstance(n, ast.Name) and n.id in ("Exception", "BaseException") for n in names)


def _reports(h: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=h.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in _REPORTING_CALLS:
                return True
            if (name == "append" and isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, (ast.Name, ast.Attribute))
                    and (getattr(fn.value, "id", None) or getattr(fn.value, "attr", None))
                    in _REPORTING_APPENDS):
                return True
        # 把异常本身交给调用方：`return f"…{e}"` / `return False, f"…{e}"`
        if isinstance(node, ast.Return) and h.name and node.value is not None:
            if any(isinstance(n, ast.Name) and n.id == h.name for n in ast.walk(node.value)):
                return True
    return False


def _offenders() -> list[str]:
    out = []
    for p in sorted(PKG.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.ExceptHandler) or not _broad(node):
                continue
            if _reports(node):
                continue
            if "#" in lines[node.lineno - 1]:
                continue                         # 写明了为什么不说
            out.append(f"{p.relative_to(PKG.parent)}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    return out


def test_every_broad_except_reports_or_says_why_not():
    bad = _offenders()
    assert bad == [], "这些 except 把异常吞得不声不响（要么说出来，要么在那一行注释为什么不说）：\n" + \
        "\n".join(bad)


def test_the_checker_itself_catches_a_silent_handler(tmp_path, monkeypatch):
    """对照：检查器认得出一个不声不响的 `except Exception: pass`。"""
    pkg = tmp_path / "media_agent"
    pkg.mkdir()
    (pkg / "m.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
                              "def h(ctx):\n    try:\n        g()\n    except Exception as e:\n"
                              "        ctx.log(f'失败 {e}')\n", encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "PKG", pkg)
    assert _offenders() == ["media_agent/m.py:4: except Exception:"]
