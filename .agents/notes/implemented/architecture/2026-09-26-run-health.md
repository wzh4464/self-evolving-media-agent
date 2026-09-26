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

## 2. 卡住检测与确认（`history.find_stuck`、`.agents/acks.json`、`media-agent ack`）

**现场**：「集位被占」每轮一条 skipped、连跳 28 轮（runloop §8a）；抓取 12 次 NameError 连着十天——每一轮
单独看都只是一条记录。第 1 节有了跨轮身份之后，"同一个问题连续好几轮都在"就能直接数出来。

**判据**（`find_stuck(state_dir, run_id, min_runs, acks)`）：

- 本轮快照里**带动作、或严重度 ≥ important** 的发现，按指纹往前数连续出现在几份 `run` 快照里；
  ≥ `STUCK_RUNS`（默认 4 = 24 小时）就是卡住。minor 且无动作的（「均在 7 天内播出，等发布即可」）挂多久都正常，
  不算。报连续轮数、从哪一轮起（`first_seen` = 连续段第一份快照的时间）、最新这一轮的摘要。
- 只数 `cmd == "run"`：手动跑几次 `diagnose` 不能让它提前升级，也不打断。
- `degraded` 的快照（扫描读 qBittorrent 不完整）不算数也不打断；本轮自己降级就不评估——残缺快照里"不在了"
  不代表解决了。
- 中间哪一轮没有它（解决过又复发），从那之后重新数。

**确认**：已知要人处理、短期不会自己好的，写进 `.agents/acks.json`（`{指纹: {reason, until?, added, what}}`），
`until` 含当天、过了重新提醒。**它放在 `.agents/` 而不在 `state/`**：和 `preferences.json` 一样是人的决定，
生产行为要能从 git 复现；在生产上改了，部署的漂移闸门会拦下它，要 `--harvest` 带回来提交。
`media-agent ack <指纹> --reason … [--until YYYY-MM-DD]` 写它（`--remove` 撤销、`--list` 列出），接受 6 位以上
的唯一前缀，指纹要在最近的发现历史里出现过（打错了会静默不起作用，所以拒绝；预先确认要写全 16 位加 `--force`），
写完打印"要提交入库"的提醒。确认文件读不了 / 不是合法 JSON 时这一轮按没有确认处理、stderr 说一句，`ack`
拒绝写（覆盖坏文件会丢掉别的确认）。

仓库里带着两条：生产 2026-09-26 run.log 里仅有的两条会升级成卡住的发现——胆大党（库内 Season 2）与
超超超超超喜欢你的100个女朋友（库内 Season 2、3）对 TMDB 单季的 `season_layout_mismatch`，抓取已对它们停手，
要人重排目录或登记 `season_offsets`。测试核对这两条的指纹与检测器此刻算出来的一致（改了指纹算法会红）。

**输出**：`run` 末尾（隔离区处置之后）一节「卡住：N 个问题连续 ≥4 轮都在」，每条带指纹和可直接复制的
`media-agent ack` 命令；确认过的只计数。

**测试**：`tests/test_stuck.py`——4 轮成立 / 3 轮不成立 / 中间断一轮重新数；摘要变了照样连续；minor 无动作的永不
卡住、带动作的会；diagnose 与降级快照不算也不打断，本轮降级不评估；确认有效 / 过期 / `until` 含当天；坏的确认
文件；仓库带的两条指纹；`STUCK_RUNS` 校验；`run` 第 4 轮起报、确认后只计数；`ack` 写文件并提醒提交、前缀、撤销、
拒绝坏输入、`--force`。

## 3. 封存冲突不再每轮撞「集位被占」

**现场**：两个不同的种子都钉着同一集、都复核通过（停滞放行换源、手动加了同钉子的种子）时，判重报
`seal_conflict`、一个都不删、等人挑（删除关口 I4）。可 `unrenamed-file` 照样给两份都提改名到同一个集位名：
一份改成了，另一份此后**每一轮**都被执行器以「集位被占」跳过——一轮一条审计、没有结论，直到人来挑
（runloop §8a：这类跳过连着 28 轮）。卡住检测看得到它，但看到的是"改名失败"而不是"封存冲突要人挑"。

**做法**：

- `builtin.seal_conflicts(show)`：集位 → 在那里冲突的封存候选（按偏好从高到低）。判据与判重的 `seal_conflict`
  **共用**：分桶 `_episode_buckets`（下完的正片视频、按 `_resolve`、按路径去重）与封存候选 `_seal_candidates`
  （钉着这一集、不是幻影、复核通过）都从 `DuplicateEpisodeDetector` 里抽出来，两边对"谁封存着这一集"不可能
  各算各的。没有钉子的集位不探测。
- `builtin.slot_holder`：集位名归**已经叫这个名字**的那份，没有就归偏好分最高的那份。已经叫这个名字的不挪——
  `_prefer_score` 先看文件名，改完名分数会变，按分数重挑就会今天归 A、明天归 B，比「集位被占」更糟。
- `unrenamed-file`：钉着这一集、所属种子是冲突里**非保留方**的文件（正片与外挂字幕），不再提改名。
- `seal_conflict` 的摘要写明集位名留在 / 给哪一份，`evidence` 多了 `slot_name_held_by`、`slot_name_goes_to`、
  `renames_held`。它按集位认指纹（第 1 节的 `subject`），连着几轮都在交给卡住检测——报一次、有结论。

**测试**：`tests/test_seal_conflict_quiet.py`——已叫集位名的那份留着、另一份不提改名；两份都是发布名时只有
偏好分最高的那份改名，连跑 4 轮零条「集位被占」、`seal_conflict` 每轮一条且指纹不变；第三份没钉的照旧判输
进隔离区；另一个种子的外挂字幕同样不提；只有一份钉着时照常改名（对照）；另一份复核不过就不是冲突。把
`unrenamed-file` 里那一句 `continue` 关掉，前四个测试红。
