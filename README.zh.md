<div align="center">

# self-evolving-media-agent

**一个会给自己补规则的番剧媒体库 agent。**

[English](README.md) | 中文

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

</div>

---

在 qBittorrent + AutoBangumi 的库上做查重、改名、TMDB 对齐、死种清理。
内置的每一条规则都来自一次真实踩坑。而当它撞上**任何现有规则都解释不了**的东西时，
它会起草一条新规则，在你的真实库上验证，验证通过才正式启用。

## 它是从哪些坑里长出来的

起点是一次持续多天的手工整理，反复撞上同几堵墙：

| 问题 | 手工时的表现 |
|---|---|
| 绕过 AutoBangumi 加的种子 | 丢 `ab:` 标签 → 永远不会被自动改名 → 每隔几天就要人工补一批 |
| 同一集有两个版本 | AutoBangumi 每 60 秒重试一次同样的改名，永不收敛 |
| 按文件名判重 | AutoBangumi 会改名，文件名两个方向都会骗你 |
| 信任 qBittorrent 的 `name` 字段 | `renameFile` 从不更新它 → 上百条"未改名"误报 |
| 目录名 ≠ TMDB 官方标题 | 刮削匹配错剧，或者什么都刮不到 |
| 死种 | 干等好几天才发现全网根本没有做种者 |

现在这些都是规则，一条命令全查出来。

## 自演进，且带牙齿

```
诊断 → 找残留（确有问题、但没有规则能动它）
     → 大模型起草声明式规则
     → 在你的真实库上做影子验证
     → 通过则写入 .agents/rules/  |  否则驳回并记录理由
```

影子验证是硬门槛，**五条全过**才能上线：

1. 确实覆盖了它本该解决的样本
2. **零误伤** —— 不能命中哪怕一个已经规范的文件
3. 命中数 ≤ 残留簇规模的 3 倍（拦截写得过宽的规则）
4. 与"已经能对这些文件给出动作"的现有规则不重叠
5. 动作参数符合执行器契约，且**不含占位值**
   （`new_name: ""`、`tmdb_id: 0`、`title: "待识别"` 一律驳回）

在一个 173 部番 / 2404 文件的真实库上首轮跑出来的实际输出：

```
🚫 驳回: unrenamed-vcb-raw
   - 命中 83 个，远超残留规模 27，规则过宽
   - 与现有规则 extras-in-library 重叠
🎉 上线: vcb-studio-sp-shorts-unrenamed      （命中 50 个，零误伤）
🎉 上线: vcb-studio-preview-shorts-unrenamed （命中 12 个，零误伤）
```

每一条提议——**上线的和被驳回的都一样**——都会写成一篇
[Agent Note](.agents/notes/)，记录推理过程、考虑过的替代方案、以及风险。
驳回记录会留着，免得下周再提一遍同样的坏主意。

### 演进产物是数据，永远不是代码

agent 产出的是**声明式 JSON**，由固定的解释器求值。它只能在封闭的字段与操作符词表里
组合条件，永远无法要求内核执行任意逻辑。

```json
{
  "id": "vcb-studio-sp-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "match": {"all": [
    {"field": "parent_dir", "op": "eq", "value": ".shorts"},
    {"field": "filename", "op": "regex", "value": "^\\[VCB-Studio\\].*\\[SP\\d{2}_\\d{2}\\].*\\.mkv$"}
  ]},
  "action": null
}
```

这不是审美偏好。这个 agent 全自动运行、有删除权限，而它操作的文件很多是
**找不回来的**（死种、早已完结的老番）。种子名和文件名都是不可信的外部输入。
在这三个条件叠加下 `exec()` 模型输出不是一个值得冒的险——完整推理写在
[这篇 Agent Note](.agents/notes/implemented/architecture/2026-08-17-declarative-rule-dsl.md) 里。

## 安全设计

即便在全自动模式下：

- **隔离区，而非删除。** 移除 = 移入 `state/trash/<日期>/`，30 天内可还原，到期才真删。
- **单轮配额。** 一轮超过 50 个文件或 200 GB？整批跳过并告警——写错的规则跑不脱缰。
- **完整审计。** 每个动作、跳过、失败都落在 `state/audit.jsonl`。
- **纵深防御。** 动作参数在提议时验一次、执行时再验一次。
- **解析失败闸。** 超过 3 个文件声称是同一集，判定为解析出错而非重复，**不删任何东西**。

最后这条不是假想。这个 agent 第一次 dry-run 时就提议删掉某季 12 集里的 11 集，
因为合集种子的所有成员文件共享同一个种子名。dry-run 拦住了它，
修复与推理[记录在这篇 bug-fix note](.agents/notes/implemented/bug-fix/2026-08-17-collection-torrent-episode-collapse.md) 里。
同样的方式还揪出了另外两个自身缺陷。**规则一定会写错——全自动之所以敢开，
是因为写错了能救回来。**

## 快速开始

```sh
git clone https://github.com/wzh4464/self-evolving-media-agent
cd self-evolving-media-agent
cp .env.example .env && chmod 600 .env   # 填入各服务地址与密钥
uv sync
```

```sh
uv run media-agent scan                # 看库里现在什么样
uv run media-agent diagnose            # 跑全部规则，只读
uv run media-agent apply --dry-run     # 预演修复
uv run media-agent apply               # 执行
uv run media-agent evolve              # 为盲区起草规则
uv run media-agent run                 # 一轮完整自治
```

建议先 `diagnose`，再 `apply --dry-run`。**读清楚它想干什么之后**，
再决定要不要把 `AUTO_APPLY` 打开。

[`deploy/`](deploy/) 里有每 6 小时跑一轮的 launchd 配置。

### 依赖的服务

| 服务 | 必需？ | 缺了会怎样 |
|---|---|---|
| qBittorrent WebUI | 是 | — |
| AutoBangumi | 可选 | 失去 `ab:` 标签相关规则和订阅感知 |
| TMDB API key | 可选 | 标题对齐规则跳过（[免费申请](https://www.themoviedb.org/settings/api)） |
| LLM（OpenAI 兼容） | 可选 | 自演进跳过，其余功能不受影响 |

基于 DeepSeek V4 Pro 开发，但任何 OpenAI 兼容的 chat 接口都能用——
改 `LLM_BASE` / `LLM_MODEL` 即可。

## 架构

一切皆插件，capability 与 provider 分离，决策沉淀为 Agent Notes。
形态借鉴 [deepseek-harness](https://github.com/deepseek-ai)。

```
media_agent/
  kernel.py       Finding/Action/Context/Registry + 声明式规则解释器
  naming.py       集号解析、归一化、画质排序——每一行都是踩出来的
  dedup.py        内容哈希（大小 + 头尾 8MB）
  scan.py         磁盘 + qBittorrent + AutoBangumi → 统一的 LibraryState
  plugins/        九条内置检测器
  actions.py      执行器 + 隔离区 + 配额 + 审计日志
  evolution.py    残留 → 提议 → 影子验证 → 提升
.agents/
  notes/          Agent Notes，路径编码 {lifecycle}/{class}/日期-标题.md
  rules/          演进出的规则（JSON），下一轮自动挂载
```

改动之前先读 [AGENTS.md](AGENTS.md)，里面有六条不容商量的约束，
每一条都是用时间换来的。

## 诚实的局限

- **是为中文字幕组的番剧发布调优的。** 集号解析覆盖的是 mikan/dmhy 各字幕组的命名习惯。
  欧美剧集大体能用，但效果不保证。
- **DSL 表达不了所有东西。** 有些真实问题用现有字段/操作符词表写不出规则。
  这些会落进 `.agents/notes/rejected/`，作为扩展词表的依据——
  而扩展词表是人的决定，永远不是模型的。
- **TMDB 被当作权威。** 如果某部番在 TMDB 上没有中文标题，
  agent 会写 NFO 锁定 TMDB ID，而不是自己猜一个。
- **只在一个库上验证过。** 173 部番、2404 文件、macOS。
  换个环境难免有毛刺——欢迎提 issue 和 PR。

## 许可

[MIT](LICENSE)
