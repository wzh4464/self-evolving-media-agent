# 删除关口：trash / drop_torrent 生效前按此刻的现场复核四条不变量

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 整改前的只读测绘把每一条删除路径逐条对照了四条不变量（下称 I1–I4），几乎每条都有
能破掉的现场；第 1 阶段只堵了最急的几处（隔离前先核对文件、死种不再搬目录、演进动作不执行），
"合集整种子作废"、"保留方已不在"、"别的种子仍声明着"、"封存因探测超时失效"都还在。
critic N5（演进规则的动作）与 N7（按 `Finding.key()` 去重会吞掉同路径的第二条）也要求：
执法点在执行器、按目标本身认。

这份笔记按提交逐节追加。

## 0. 四条不变量

- **I1** 不让任何 (季, 集) 集位变成零个可播文件。
- **I2** 不删另一个保留着的种子仍然声明的路径。
- **I3** 不为了去掉一个文件整种子作废多文件种子。
- **I4** 不删封存了集位的文件（`ma:` 钉子 + 复核通过），封存要稳定：探测不可用不能让它失封。
- 外加：演进规则（LLM 提议的 DSL）产出的删除一律不执行。

**为什么在执行器、在动手前那一刻**：检测器的判断基于诊断时的一份快照，删除发生在几分钟之后，
中间还夹着同一批的其它动作（`_OP_ORDER`：摘种子 op 1、隔离 op 5、改名 op 6）。只有执行器知道
"本批次已经删了什么"、只有那一刻的 qBittorrent 与磁盘是真的。**按目标认**（路径 + 种子 hash），
不按产出它的规则、也不按 `Finding.key()`。

**拒绝记 skipped、不记 failed**：理由以「删除关口：Ix」开头。它们不是执行失败，记成 failed
会喂进 `find_failure_patterns`，被当成"规则本身有问题"。看不全（qBittorrent 读失败）仍记 failed
「无法确认…占用情况，未做任何改动」，与占用闸门（`architecture/2026-09-26-path-claims.md`）同口径。

## 1. 模块与接线；先接上 I2、I3 与演进规则

**`media_agent/gate.py`**：

- `screen(finding) -> str`：不看现场就能拒绝的——`evidence.origin == "dsl"`。第 1 阶段在
  `Executor._dispatch` 拦下所有演进动作，那道保留作纵深防御；删除的执法点是这里。
- `disposition_of(finding)`：`duplicate` / `extras` / `bundled_version` / `dead_partial` /
  `manual` / `other`。**只看产出它的规则**（内置检测器的 id / kind），不看动作参数——处置类别
  决定 I1 的例外，参数可以由任何人写，不能自己给自己放行。
- `check_trash(executor, finding, path) -> Verdict`：`path` 是盘上真实存在的普通文件（调用方
  已核过）。`Verdict` 的 `refused` → skipped，`failed` → failed，否则按 `torrent_hash` /
  `file_only` / `entry` 处置种子再搬文件。
- `Verdict.audit()` → 每条 trash 记录的新字段 `deletion`（见第 1.3 节）。

**`_op_trash` 的顺序**：`screen` → 路径形状（媒体库里、存在、不是目录、普通文件）→
`check_trash` → 配额 → dry-run → 按关口的结论处置种子 → 搬文件。不在盘上的（幻影、还没下完的
特典）照旧走 `_trash_absent`，只处置种子那一侧。

### 1.1 I3：多文件种子只作废这一个条目

以前 `file_only` 全凭检测器自报：duplicate-episode 的普通输家从来不设，执行器就整种子作废。
合集里一集判重输了，其余十几集失去做种；`torrent_record_lost` 而且没有 magnet，回退加不回来。
生产审计 63 条 duplicate-episode 的整种子作废（例：3年Z组银八老师 [01-12]，12 个文件）。

现在按**此刻**的 `files()`：要下载（优先级非 0）的文件多于一个 → 自动降级成只作废这一个条目
（设为不下载 + 搬进隔离区），`deletion.notes` 记一句「I3：…」；只剩这一个 → 整种子作废
（把唯一的文件设为不下载会留下一个空种子，被 stale-torrent-path 报成死链——第 1 阶段就有的退化，
理由见 `bug-fix/2026-08-19-file-only-orphan-torrents.md`）。条目按 `save_path + 条目名` 的完整
路径认；要删的路径根本不是这个种子的条目 → failed（以前照样整种子摘掉、再搬一个不相干的文件）。
不在盘上的幻影输家同理：合集里只把这个条目设为不下载，不摘整个合集。

降级而不是拒绝：检测器要的是"这个文件别在库里"，降级正好做到，而且可回退（逆操作照旧
`restore_from_trash`；优先级不自动恢复——第 1 阶段就有的限制）。

### 1.2 I2：另一个保留着的种子仍声明这个路径

`scan` 每个路径只出一条 MediaFile，多个种子声明同一路径时只留"分数最高"的那个，其余看不见。
要删的若是单集种子 L 的那一份，而合集 P 也列着这个文件：以前整种子摘掉 L、把文件搬走——
P 从此缺一集，qBittorrent 会重下或报 missingFiles（2026-09-06 尼古喵喵 S01E08 是同一形态）。

现在 `claims.check(路径, own_hash=要处置的种子, own_path=路径, disk=False)`：豁免这次动作自己
那个条目；本批次已摘的种子不算（`ignore`）；优先级 0 的条目不算。`.!qB` 半成品按它的正名问
（别的种子要写的是 `X`，盘上是 `X.!qB`）。诊断里是纯本地文件（`torrent_hash` 为空）、此刻却有
种子声明着，同样拒绝。

### 1.3 审计：`deletion` 字段（给 purge 用）

purge 要判断"隔离区里的这个能不能真删"，需要删除那一刻的事实——之后种子可能已经摘了、标签
与分类都查不到了（整改前的测绘：68 条 trash 记录 `torrent_record_lost`，钉子、分类随之丢失）。
每条 trash 记录（applied / skipped / failed，含幻影与还没下完的条目）加：

    "deletion": {
      "gate": "passed" | "I1" | "I2" | "I4" | "evolved" | "unknown" | "mismatch",
      "disposition": "duplicate" | "extras" | "bundled_version" | "dead_partial" | "manual" | "other",
      "slot": [季, 集] | null,          # 动作给的 slot > 种子上的 ma: 钉子 > 名字
      "keeper": {...} | null,           # 判重的保留方（后续提交填）
      "subject": {"torrent_hash", "name", "pin", "tags", "category", "torrent_files"},
      "notes": [...]                    # 可选：I3 降级等
    }

新键，旧记录（9,619 行）没有它，读的人必须容忍缺失；原有的 `trashed_to` / `freed_bytes` /
`undo` 一个不变。规则 id 就是记录本身的 `rule`。

**测试**：`tests/test_deletion_gate.py`——I2：合集仍声明着的路径、纯本地却被种子声明的路径都
拒绝且快照不变；邻居 `files()` 读不到 failed「无法确认…未做任何改动」。I3：12 集合集里的一集
只作废这一个条目、其余 11 集照常；按完整路径认对 `a/`、`b/` 下同名的条目；单文件种子照旧整个
作废；路径不是这个种子的条目 failed；合集里的幻影输家只设为不下载。演进规则：绕过分派直接调
`_op_trash` 也被拒。审计：`deletion` 的形状；处置类别只看规则不看参数。对旧执行器跑：执行器层面
的 10 条全红。`tests/test_same_batch.py` 的"同批次第二次隔离"改了现场：旧现场（合集里一集判重
输了就整种子作废）正是 I3 堵上的洞，新现场是"合集里只剩一集要下载"，并断言整种子摘掉之后不再
去问它的 `files()`。
