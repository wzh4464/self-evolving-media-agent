"""机械查找重复实现：同名函数、相同函数体结构、重复正则字面量、重复常量集合。

**为什么需要它**：2026-09-07 发现「`Season N` 目录 → 季号」在 scan / purge /
grab / subscription / builtin 里写了五遍、四种行为，`Season  2` 和 `season 3`
在扫描侧认不出、抓取侧认得出。这类问题肉眼审不出来——同一个功能在不同文件里
换个变量名、换个 `re.match`/`re.search` 就认不出是同一件事了。

跑法：python3 tools/audit_duplication.py（从仓库根目录）

C 组（重复正则）信噪比最高：同一个正则字面量出现在多个文件，几乎总是意味着
同一个概念被解析了两遍，而两遍迟早会漂移。
"""
import ast, hashlib, re, collections
from pathlib import Path

ROOT = Path("media_agent")
files = sorted(ROOT.rglob("*.py"))

funcs = collections.defaultdict(list)     # name -> [(file, lineno, bodyhash, nlines)]
bodies = collections.defaultdict(list)    # normalized-body hash -> [(file, name, lineno)]
regexes = collections.defaultdict(list)   # pattern -> [(file, lineno)]
strsets = collections.defaultdict(list)   # frozenset of str literals -> [(file, name)]

def norm(node):
    """函数体去掉 docstring / 变量名后的结构指纹。"""
    body = [n for n in node.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    if not body:
        return None
    dump = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    dump = re.sub(r"'[A-Za-z_][A-Za-z0-9_]*'", "'V'", dump)   # 抹掉标识符名
    return hashlib.sha1(dump.encode()).hexdigest(), len(body)

for f in files:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            r = norm(node)
            if r and r[1] >= 2:
                funcs[node.name].append((str(f), node.lineno, r[0], r[1]))
                bodies[r[0]].append((str(f), node.name, node.lineno, r[1]))
        # re.compile("...") 与 re.match/search/fullmatch("...", ...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
           and getattr(node.func.value, "id", "") == "re" \
           and node.func.attr in ("compile", "match", "search", "fullmatch", "findall", "sub"):
            if node.args and isinstance(node.args[0], ast.Constant) \
               and isinstance(node.args[0].value, str):
                regexes[node.args[0].value].append((str(f), node.lineno))
        # 模块级字符串集合常量
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            vals = [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(vals) >= 3:
                name = getattr(node.targets[0], "id", "?")
                strsets[frozenset(vals)].append((str(f), name, node.lineno))

print("=== A. 同名函数在多个模块重复定义 ===")
hit = 0
for name, locs in sorted(funcs.items()):
    seen_files = {l[0] for l in locs}
    if len(seen_files) > 1:
        hit += 1
        same = len({l[2] for l in locs}) == 1
        print("  %-24s %s  %s" % (name, "结构相同！" if same else "结构不同",
              ", ".join("%s:%d" % (l[0], l[1]) for l in locs)))
print("  命中 %d 组" % hit)

print("\n=== B. 函数体结构完全相同（不同名也算）===")
hit = 0
for h, locs in bodies.items():
    if len({(l[0], l[1]) for l in locs}) > 1 and locs[0][3] >= 3:
        hit += 1
        print("  %d 行体，%s" % (locs[0][3],
              ", ".join("%s:%s():%d" % (l[0], l[1], l[2]) for l in locs)))
print("  命中 %d 组" % hit)

print("\n=== C. 同一正则字面量出现在多处 ===")
hit = 0
for pat, locs in sorted(regexes.items()):
    if len({l[0] for l in locs}) > 1:
        hit += 1
        print("  %-40r %s" % (pat[:38], ", ".join("%s:%d" % l for l in locs)))
print("  命中 %d 组" % hit)

print("\n=== D. 同一组字符串常量定义了多次 ===")
hit = 0
for s, locs in strsets.items():
    if len(locs) > 1:
        hit += 1
        print("  %s → %s" % (sorted(s)[:4], ", ".join("%s:%s" % (l[0], l[1]) for l in locs)))
print("  命中 %d 组" % hit)
