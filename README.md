# media-agent

番剧媒体库的自治治理 agent：**查重、改名、TMDB 对齐、死种清理**，
并且能发现自身规则的盲区、提议新规则、验证通过后自动上线。

跑在 `zihan_air` 上，直连本机的 qBittorrent 与 AutoBangumi。

## 它解决什么

这套东西是从一次持续多天的手工整理里长出来的。那次整理反复撞上同几类问题：

| 问题 | 手工时的表现 |
|---|---|
| 手动加种子绕过 AutoBangumi | 丢 `ab:` 标签 → 永远不会被自动改名 → 每隔几天就要人工补一批 |
| 同集多版本抢同一文件名 | AutoBangumi 每 60 秒重试一次改名，永不收敛，日志刷屏 |
| 按文件名判重 | AutoBangumi 会改名，文件名不可靠，漏判误判都有 |
| 用 qBit 的 `name` 字段判断改名状态 | `renameFile` 不更新该字段 → 上百条误报 |
| 目录名不是 TMDB 官方标题 | 刮削失败或匹配错剧 |
| 死种 | 干等几天才发现全网 0 做种 |

现在这些都是规则，跑一条命令就查完，且**能自己长出新规则**。

## 用法

```sh
uv run media-agent scan               # 看库现状
uv run media-agent diagnose           # 跑全部规则出问题清单（只读）
uv run media-agent apply --dry-run    # 预演修复
uv run media-agent apply              # 执行修复
uv run media-agent apply --kind orphan_torrent   # 只修某一类
uv run media-agent evolve             # 为规则盲区提议新规则
uv run media-agent run                # 完整自治轮次（launchd 每 6 小时跑这个）
```

## 自演进怎么工作

```
诊断 → 找残留（有问题但没规则能动它）→ deepseek 提议声明式规则
     → 影子验证 → 通过则写入 .agents/rules/ 并立即生效
                → 不通过则记入 .agents/notes/rejected/
```

**影子验证是硬门槛**，四条全过才能上线：
1. 确实命中了它本该解决的样本
2. **零误伤**——不命中任何已规范的文件
3. 命中数不超过残留簇规模的 3 倍（防规则写得过宽）
4. 与现有规则无重叠（只算"能给出动作"的规则）
5. 动作参数符合执行器契约且**不是占位值**（`new_name:""`、`tmdb_id:0` 一律驳回）

实测第一轮：3 条提议 → 1 条因过宽+重叠驳回，2 条上线（命中 50/12 个，零误伤）。

演进产物是**声明式 JSON**，不是生成的代码——理由见
[声明式规则 DSL](.agents/notes/implemented/architecture/2026-08-17-declarative-rule-dsl.md)。

## 安全设计

全自动模式（`AUTO_APPLY=true`）下仍有三层保护：

1. **隔离区**：删除 = 移入 `state/trash/<日期>/`，30 天后才真删，期间可整体还原
2. **配额上限**：单轮删除超过 50 个文件或 200GB 就整体跳过并告警
3. **审计日志**：每个动作（含跳过与失败）落 `state/audit.jsonl`

外加两处纵深防御：动作参数在验证层和执行层各查一次；同一集出现 >3 个文件时
判定为解析异常而非重复，绝不产出删除动作。

## 配置

```sh
cp .env.example .env && chmod 600 .env   # 填入各服务的地址与密钥
uv sync
```

需要的外部服务：qBittorrent WebUI、AutoBangumi、TMDB API key、
LLM（openlux 上的 deepseek-v4-pro）。缺 TMDB key 时标题对齐规则自动跳过，
缺 LLM key 时自演进跳过，其余功能不受影响。

## 想改动的话

先读 [AGENTS.md](AGENTS.md)，里面列了六条不可动摇的约束——每条都是踩坑换来的。
非平凡改动要在同一次提交里写 Agent Note。
