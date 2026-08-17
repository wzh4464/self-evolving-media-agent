# AGENTS.md

media-agent 是一个**自治的番剧媒体库治理 agent**：查重、改名、TMDB 对齐、死种清理，
并且能发现自身规则的盲区、提议新规则、验证后自动上线。

形态借鉴 [deepseek-harness](../deepseek-harness/AGENTS.md)：**一切皆插件**，
capability 与 provider 分离，决策沉淀为 Agent Notes。

## 仓库结构

```
media_agent/
  kernel.py       插件内核：Finding/Action/Context/Registry + 声明式规则 DSL 解释器
  config.py       .env 配置加载
  naming.py       命名规则：集号解析、归一化、画质排序 —— 每条都对应一次实际踩坑
  dedup.py        内容哈希（大小 + 头尾 8MB）
  cache.py        TMDB/哈希/LLM 判断的磁盘缓存
  scan.py         三方状态汇总成 LibraryState
  clients.py      capability 的 provider 实现（qBit/AutoBangumi/TMDB/AniList/LLM）
  plugins/        内置检测器
  actions.py      执行器 + 隔离区 + 配额上限 + 审计日志
  evolution.py    自演进：残留检测 → 提议 → 影子验证 → 提升
  cli.py          命令行入口
.agents/
  notes/          Agent Notes，路径编码 {lifecycle}/{class}/日期-标题.md
  rules/          演进出来的声明式规则（JSON），下轮自动挂载
state/            运行时数据：审计日志、隔离区、缓存（gitignore）
```

## 命令

```sh
uv run media-agent scan               # 看库现状
uv run media-agent diagnose           # 跑全部规则，出问题清单（只读）
uv run media-agent apply --dry-run    # 预演修复
uv run media-agent apply              # 执行修复
uv run media-agent evolve             # 为规则盲区提议新规则
uv run media-agent run                # 完整自治轮次
```

## 不可动摇的约束

这些是踩坑换来的，改动前必须先读对应 Agent Note：

1. **判重只认内容哈希，绝不认文件名。** AutoBangumi 会改名，文件名不可靠。
2. **事实来源分两种。** 有种子的内容以 `torrents/files` 为准——那是 qBittorrent
   实际会写入的路径，`renameFile` 后同步更新，且包含尚未落盘的文件；
   无种子的纯本地文件才以磁盘为准。
   **不要用 `torrents/info` 的 `name` 字段**：那是种子*显示名*，`renameFile`
   后不变，拿它判断会产生上百条误报。
3. **所有改动必经 qBittorrent。** 有种子的文件改名走 `renameFile`，种子里找不到
   该文件就报失败中止，**绝不退化成文件系统 `mv`**；目录改名由 `setLocation`
   让 qBittorrent 自己搬运，不要 `Path.rename` 整个目录。库里现存的 28 个死链
   种子就是早先违反这条留下的（已用 `relink_torrent` 全部修复）。
4. **删除 = 移入隔离区**，不是 `rm`。全自动模式的前提就是这一条。
   同理，**每个改动状态的动作都必须记录逆操作**，否则 `rollback` 救不回来。
5. **演进产物是声明式 DSL，永不 `exec()` 模型生成的代码。**
   见 [声明式规则 DSL](.agents/notes/implemented/architecture/2026-08-17-declarative-rule-dsl.md)。
6. **订阅番剧必须走 AutoBangumi 自己的 API**，手动往 qBittorrent 加种子会丢
   `ab:` 标签、永久脱离自动改名管辖。
7. **修订阅时三步顺序不能反**：先改 `title_aliases`/`rss_link` → 再清"已登记但
   不在 qBittorrent"的 torrent 记录 → 最后刷新。`pull_rss` 只处理 `check_new()`
   筛出的新条目，顺序反了会让 AutoBangumi 用**仍然失效**的规则把条目重新登记一遍。

## 自演进的闭环

```
诊断 → 残留检测 → LLM 提议规则 → 影子验证 → 通过则上线 / 否则驳回
                                     ↓              ↓
                              implemented/     rejected/
```

影子验证是硬门槛，全部满足才能上线：
- 确实命中了它本该解决的样本
- **零误伤**——不命中任何已规范的文件
- 命中范围不超过残留簇规模的 3 倍（防止规则写得过宽）
- 与现有规则不重叠

驳回的提议也留档在 `rejected/`，防止后续重复提同样的坏主意。

## 写 Agent Note 的时机

任何非平凡改动都要在同一次提交里新增或更新一条 Agent Note：行为变化、架构决策、
跨文件契约、流程工具、磁盘/配置格式。类别取自闭集：
`feature` / `bug-fix` / `simplification` / `architecture` / `process` / `testing`。
