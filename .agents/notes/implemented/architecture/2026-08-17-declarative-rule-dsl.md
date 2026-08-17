# 自演进产物用声明式 DSL，不用生成代码

Status: implemented
Class: architecture

## 现象

这个 agent 要能"自己发现问题并演进"，最直接的实现是让模型生成 Python 检测器代码、
`exec()` 进运行时。deepseek-harness 的 `packages/skill` + 自挂载插件形态也确实支持
运行时装载新插件。

但本 agent 有两个前提让"执行生成代码"不可接受：

1. **它跑在全自动模式**，用户明确要求包括删除在内的动作都不需要确认。
2. **它操作的是用户不可再生的个人媒体文件**（很多是已完结老番的死种，删了就找不回来）。

模型生成的代码一旦有逻辑错误或被 prompt injection 污染（种子名、文件名都是外部
不可信输入，实测里就有 `[Nekomoe kissaten&LoliHouse]` 这类带特殊字符的名字），
配合全自动删除权限，后果不可逆。

## 决定

演进产物是**声明式规则 JSON**，由 `kernel.py` 里的固定解释器求值：

```json
{
  "id": "detect-nc-op-in-season-dir",
  "kind": "extra_in_library",
  "severity": "minor",
  "match": {"all": [
    {"field": "filename", "op": "regex", "value": "NCOP|NCED"},
    {"field": "parent_dir", "op": "regex", "value": "^Season \\d+$"}
  ]},
  "action": {"op": "trash"}
}
```

模型只能在 `_FIELD_GETTERS`（文件名/目录/大小/标签/种子状态…）和 `_OPS`
（regex/eq/gt/contains/glob…）两个封闭词表内组合条件，无法要求内核执行任意逻辑。
动作 op 同样是封闭集合，且全部经过 `Executor` 的隔离区与配额保护。

规则可读、可 diff、可人工审查、可单独禁用（`"enabled": false`），
存在 `.agents/rules/*.json` 里跟着 git 走。

## 放弃的替代方案

- **exec 生成的 Python**：表达力最强，但如上所述在全自动 + 不可再生数据下不可接受。
- **生成代码但沙箱执行**：Python 沙箱本身不可靠，且要防的是逻辑错误而不只是逃逸。
- **只提议、永远人工审查**：违背"自演进"的诉求，也退化成又一个待办清单。
- **让模型直接给出要删除的文件列表**（不立规则）：一次性、不积累、不可复用，
  且失去了"零误伤"这道可机械验证的门槛。

## 风险与缓解

**DSL 表达力不足，某些真实问题写不出规则。**
缓解：这类残留会在 `evolve` 时得到 `worth_a_rule=false` 或验证失败，
留在 `rejected/` 里作为扩展 DSL 词表的依据——词表扩展是人工决策，不是模型自决。

**规则写得过宽，误伤已规范文件。**
缓解：影子验证强制"零误伤 + 命中数不超过残留规模 3 倍"，两条任一不满足即驳回。

**规则之间冲突。**
缓解：注册表按注册顺序去重（内置规则永远先于演进规则注册），
且验证阶段显式检查与现有规则的命中集是否相交。
