# "悄悄停摆"必须被看见：发现历史、卡住检测与每轮健康报告（整改第 3 阶段，逐节追加）

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 这个项目最贵的几次事故都是同一种形态——**每一轮单独看都"正常"，放在一起看才发现在原地打转或已经停了**：

- 入间同学空转三天：抓取模型上线后每轮全库只补一集，每轮 diagnose 规规矩矩报一条、apply 也成功
  （`kernel.Finding.key` 的注释）；
- 2026-09-16 … 09-26 抓取 0 次成功：12 次 NameError 全记在 audit.jsonl 里，run.log 只有一行汇总，
  launchd 的 last exit code 一直是 0；
- 「集位被占」每轮一条跳过，连着 28 轮（runloop §8a 的统计），没有任何地方把它们连起来；
- run.log 1.8 MB、没有时间戳、没有批次号、不轮转（runloop §4）：出了事翻日志都对不上是哪一轮。

这份笔记记录第 3 阶段"可观测"的设计：每轮的发现落历史、同一问题连续几轮都在就升级成"卡住"、每轮
收尾写一份健康报告并按变化发通知，外加日志时间戳 / 轮转、维护暂停。按提交逐节追加。

## 1. 发现历史与稳定指纹（`media_agent/history.py`）

**现场**（critic N11）：没有动作的发现——`pending_ownership`、`seal_conflict`、`season_layout_mismatch`、
抓取的"找不到番组页"——以前只以文字打进 run.log；没有路径的发现去重键退回 `(show, summary)`，而摘要里嵌着
计数（「S01 缺 3 集」下一轮「缺 2 集」、「竟有 4 个文件声称是同一集」），跨轮根本认不出是同一个问题。

**做法**：

- `run` 与 `diagnose` 每轮把**全部**发现（含无动作、已归类的）写成 `state/findings/<run_id>.jsonl`：第一行
  header（run_id、ts、cmd、degraded、条数），其后一条发现一行。一轮什么都没发现也写 header——"连续几轮都有"
  要靠空的那一轮来断。先写临时文件再改名；只留最近 60 份（`KEEP_RUNS`，6 小时一轮是 15 天）。
- **指纹** = sha1(规则, 类型, 目标) 的前 16 位，**永远不含摘要**。目标取第一个有的：`show#subject` > 路径 >
  `torrent:<hash>` > show。
- `Finding.subject`（新字段，默认空）：检测器声明"这是关于哪一集 / 哪一季"。集位级的发现（`phantom_only`、
  `seal_conflict`、`pending_ownership`、`suspicious_episode_parse`）的 `path` 只是桶里第一个文件——多一个文件、
  改一个名，第一名就换了；缺集 / 可抓取这类根本没有路径。现在它们分别带 `S01E08` / `S02`，空分类带
  `分类:<名>`。只写身份、不写计数；不参与 `key()` 去重（不改变任何现有行为）。
- `run` 的批次 ID 在开头就定下来（`new_run_id()`），执行器、隔离区处置、发现历史共用——审计与发现历史对得上。
- **写永不抛异常**：写不进去（`state/findings` 被一个文件占了、磁盘满）只在 stderr 说一句「发现历史：…」，
  这一轮照常。观测不能拦正事。
- 扫描读 qBittorrent 不完整的一轮，快照标 `degraded`（卡住检测不拿残缺快照算连续，见第 2 节）。

**测试**：`tests/test_findings_history.py`——摘要里的计数变了指纹不变；真检测器
（`suspicious_episode_parse`）多一个文件、换了第一名指纹不变；目标的取值顺序；空轮写 header；保留 60 份；
写不进去不抛、`run` 照样返回 0；`run` / `diagnose` 各写一份、`run` 的快照与审计同一个批次 ID；降级的一轮标
`degraded`。
