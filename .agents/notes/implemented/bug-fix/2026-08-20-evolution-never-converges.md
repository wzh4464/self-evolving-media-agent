# 自演进从设计上无法收敛，累积出 28 条同质规则

Status: implemented
Class: bug-fix
Rule: `evolution.find_residue` / `kernel.RuleSpec`

## 现象

`.agents/rules/` 里堆了 28 条规则，名字自己就说明了问题：

```
rezero-s2-vcb-nced-sp-unrenamed.json
rezero-s2-vcb-nced-sp-unrenamed-v2.json
rezero-s2-vcb-nced-sp-unrenamed-residual.json
rezero-s2-vcb-nced-sp-leftover.json
rezero-s2-vcb-nced-sp03-sp05-final.json
rezero-s2-vcb-residual-nced-sp.json
majo-no-tabitabi-preview-shorts-unrenamed{,-v2,-v3}.json
majo-no-tabitabi-preview-shorts-untracked{,-v4}.json
…
```

28 条全部集中在两个 kind（`special_episode_unrenamed` 19 条、
`preview_episode_unrenamed` 9 条），针对的是同一批文件。
每轮 `evolve` 都会再生一条 `-v3` / `-final` / `-residual`。

## 根因

`evolution.py` 的残留判据：

```python
explained = {f.path for f in findings if f.path and f.action is not None}
```

**只有带 action 的 finding 才算"已解释"。** 而实测：

```
28 条演进规则中，有 action 的： 0 / 28
```

于是构成一个必然的死循环：

1. 残留检测扫出那批文件（无 action ⇒ 不算已解释）
2. 模型提议一条新规则去匹配它们，`action: null`
3. 规则通过校验、挂载、正常触发——**但永远不会减少残留**
4. 下一轮，同一批文件再次被扫出，再提一条正则略有出入的新规则

**这个 bug 是修上一个 bug 时引入的。** 早先模型提议过带坏 action 的规则
（`new_name: ""`、`tmdb_id: 0`、`title: "待识别特典"`），于是加了参数校验并把
提示词改成"action 是可选的，算不出确切值就别给"。模型学会了安全地不给 action
——恰好踩中收敛判据的死角。修掉一个，制造了另一个。

## 修复

区分**两种"没有动作"**，这是问题的核心：

- **已归类**：规则看懂了这批文件，只是不存在值得自动执行的动作
- **未解决**：只是标记出来，问题仍悬而未决

新增 `RuleSpec.resolution`（`classified` | `unresolved`，默认 `classified`）
与 `Finding.classified`。残留判据改为：

```python
explained = {f.path for f in findings
             if f.path and (f.action is not None or f.classified)}
```

**默认取 `classified` 而不是 `unresolved`**：一条规则既然写得出精确的匹配条件，
就说明它已经理解了这批文件。默认成 `unresolved` 等于保留原有的不收敛行为。

`_overlaps` 也一并纳入 classified 作为第二道闸——残留过滤本已挡住这些文件，
这里再挡一次，免得漏过上游又生出同质规则。内置检测器不置 `classified`，
所以 `unrenamed-file` 那类"检测到但解析不出集号"的空白仍然可被演进器填补，
这一点没有退化。

## 验证

29 条现存规则在新语义下全部被判定为已归类（29/29），残留随之清空。

## 教训

**收敛判据必须与产出形态匹配。** 演进器接受"只分类不动手"的规则，
它的收敛判据却只认动作——接受的东西和认可的东西不是一回事，
循环就必然空转。而且空转不报错、不告警，只是安静地每轮多写一个文件，
直到有人去数 `.agents/rules/` 里有多少条才发现。

**自演进系统的失败模式是沉默的**：它一直在"工作"，产出看起来合理的规则，
命中数、零误伤这些指标也都正常——单看任何一轮都没问题，
只有看累积形态（`-v2` `-v3` `-residual` `-final`）才暴露。
这类系统需要一个"我是否在原地打转"的自检，而不只是逐轮的质量校验。
