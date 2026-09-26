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
  复审时补上漏掉的 `rename_collision`（critical、没有动作，正是卡住 / 确认的对象）：它的 `path` 同样是桶里第一个
  文件，而有种子的文件按 `torrents()` 的顺序排——qBit 5.x 的 /torrents/info 不排序（`SessionImpl::torrents()` 遍历
  `QHash`，进程重启换种子、加了种子重新散列都会变），看门狗重建一次容器指纹就换了。现在带 `S01E08`。其余以
  `files[0]` / `sealed[0]` 为 path 的发现（`phantom_only`、`seal_conflict`、`pending_ownership`、
  `suspicious_episode_parse`）都已带 `subject`；以单个文件为对象的（判输的那份、同名字幕）按它自己的路径认。
- `run` 的批次 ID 在开头就定下来（`new_run_id()`），执行器、隔离区处置、发现历史共用——审计与发现历史对得上。
- **写永不抛异常**：写不进去（`state/findings` 被一个文件占了、磁盘满）只在 stderr 说一句「发现历史：…」，
  这一轮照常。观测不能拦正事。
- 扫描读 qBittorrent 不完整的一轮，快照标 `degraded`（卡住检测不拿残缺快照算连续，见第 2 节）。

**测试**：`tests/test_findings_history.py`——摘要里的计数变了指纹不变；真检测器
（`suspicious_episode_parse`）多一个文件、换了第一名指纹不变；`rename_collision` 在 `torrents()` 倒序后指纹不变
（改之前红）；目标的取值顺序；空轮写 header；保留 60 份；
写不进去不抛、`run` 照样返回 0；`run` / `diagnose` 各写一份、`run` 的快照与审计同一个批次 ID；降级的一轮标
`degraded`。

## 2. 卡住检测与确认（`history.find_stuck`、`.agents/acks.json`、`media-agent ack`）

**现场**：「集位被占」每轮一条 skipped、连跳 28 轮（runloop §8a）；抓取 12 次 NameError 连着十天——每一轮
单独看都只是一条记录。第 1 节有了跨轮身份之后，"同一个问题连续好几轮都在"就能直接数出来。

**判据**（`find_stuck(state_dir, run_id, min_runs, acks)`）：

- 本轮快照里**带动作、或严重度 ≥ important** 的发现，按指纹往前数连续出现在几份 `run` 快照里；
  ≥ `STUCK_RUNS`（默认 4 = 24 小时）就是卡住。minor 且无动作的（「均在 7 天内播出，等发布即可」）挂多久都正常，
  不算。报连续轮数、从哪一轮起（`first_seen` = 连续段第一份快照的时间）、最新这一轮的摘要。
- **动作每轮都成功、问题每轮被别人重新造出来的，不算**（`_RECURRING_KINDS`，复审时补）：`empty_category`。AutoBangumi
  每加一集就重建它的下载分类 `Bangumi`，category-consolidation 每轮删、每次成功（生产 14 天 25 次、全部 78 次，
  2026-09-20 起连续 4 轮）。复审把生产 run.log 按批次对齐审计回放两周：带着仓库里的两条确认，只会发出一条「新卡住」，
  就是它（指纹 `6d1a114717a22386`，40 天的日志里会报 5 次），人除了 ack 什么也做不了。选了按类型排除而不是再往
  `acks.json` 里写一条：确认是人的决定，这里是判据本身错了——带动作不等于没收敛；删不掉的照样进失败与
  "反复失败"（第 6 节），不会因此看不见。同样的回放里其余的信约 10–12 封 / 14 天（失败动作的 ok → warn、两次 qBit
  超时的 critical 与恢复），没有种子数与磁盘告警。
- 只数 `cmd == "run"`：手动跑几次 `diagnose` 不能让它提前升级，也不打断。
- **预演的 run 对带动作的发现同理**（复审时补）：快照 header 记 `dry_run`，预演的一轮对带动作的发现不算数也不打断
  ——动作根本没被尝试，它还在说明不了"没收敛"。以前 4 轮 `run --dry-run` 就把一个从没执行过的 `write_sidecar`
  报成卡住、发信（`AUTO_APPLY` 的默认值是 false，新装的预览模式一天后就会这样）；人在两轮 launchd run 之间手动预演
  一次，也让真实的连续段提前一轮升级。没有动作的（布局对不上、封存冲突）与执不执行无关，预演照常算数——所以不是
  简单地把预演当 `diagnose`：那样预览模式下连要人处理的问题也永远不会被提醒。也不是"预演里只数没动作的"：那会
  让手动预演**打断**真实连续段（带动作的在那份快照里"不算"就等于不在）。
- `degraded` 的快照（扫描读 qBittorrent 不完整）不算数也不打断；本轮自己降级就不评估——残缺快照里"不在了"
  不代表解决了。
- 中间哪一轮没有它（解决过又复发），从那之后重新数。
- 每份快照先收成"算数的指纹"集合再数（复审时改的）：逐条比是 发现数² × 轮数，库里一乱（几千条发现）就是上亿次比较；
  3000 条 × 60 轮实测 0.25 秒。

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

**测试**：`tests/test_stuck.py`——4 轮成立 / 3 轮不成立 / 中间断一轮重新数；预演：4 轮预演不让带动作的卡住、
3 真 + 1 预演不算 4 轮也不打断（再一轮真的就是 4）、AB 重建的空分类（真检测器、生产那条指纹）连续 6 轮不算卡住、没有动作的照常卡住、端到端 4 轮 `run --dry-run` 不报（去掉
`continue` 或不往快照里记预演，各红一个）；摘要变了照样连续；minor 无动作的永不
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
- `rename-collision`（critical，"AutoBangumi 改名死循环"）同样不再为这组冲突报：争这个名字的恰好就是冲突里的那几个
  封存种子时跳过——它们都是本项目抓的、在剧名分类下，AB 查不到，谈不上死循环；而判重已经报了 `seal_conflict`。
  以前同一个冲突每轮两条（一条 critical），卡住检测里也是两条（写健康报告的测试时发现的）。有没钉的第三份一起争
  照报——判重这一轮会清掉它。

**测试**：`tests/test_seal_conflict_quiet.py`——已叫集位名的那份留着、另一份不提改名；两份都是发布名时只有
偏好分最高的那份改名，连跑 4 轮零条「集位被占」、`seal_conflict` 每轮一条且指纹不变；第三份没钉的照旧判输
进隔离区；另一个种子的外挂字幕同样不提；只有一份钉着时照常改名（对照）；另一份复核不过就不是冲突。把
`unrenamed-file` 里那一句 `continue` 关掉，前四个测试红。`rename-collision`：封存冲突不再报、有没钉的第三份照报。

## 4. Registry 记下被吞的检测器异常

**现场**（critic N9）：`Registry.run_all` 接住检测器的异常只打一行 `[registry] 规则 X 执行失败: …`，就没了。
单条规则崩溃不拖垮整轮是对的，但一个 "database is locked" 让判重在半路停下，这一轮的诊断就静默地少了一截，
没有任何计数。

**做法**：`Registry.errors`——最近一次 `run_all` 里被吞的异常 `{rule, error: "类型: 消息", where}`，`where` 是最后
三层调用的 `文件:行 函数`（由内向外）。每次 `run_all` 重新开始计。日志那一行的开头不变（tripwire 与读
run.err.log 的人认它），末尾加上位置。健康报告（第 6 节）据此列出"哪条规则崩了、崩在哪"。

**测试**：`tests/test_registry_errors.py`——崩之前产出的与别的规则的发现照常返回；记下规则、异常、位置；
每次 `run_all` 重新计；没崩就是空列表。

## 5. 种子数合理性：`torrents()` 成功了、却少了一大截（`health.torrent_count_problem`）

**现场**（第 1 阶段遗留，`scan.build_state` 的注释）：第 1 阶段只挡住了"登录成功却 0 个种子"——那个判据不需要
跨轮状态。部分缺失（qBit 容器重建时 BT_backup 只恢复了一部分，会话里只剩 300 个而不是 539 个）在接口上同样没有
任何报错，后果却与 LAT-01 一样：缺了种子的文件全成了"纯本地文件"，改名走文件系统、隔离跳过种子。

**判据**：`上一轮的数 − 此刻的数 − 审计里记着的本项目摘除数 > max(TORRENT_DROP_MIN=20, ⌈上一轮 × TORRENT_DROP_PCT=10%⌉)`
→ 这次扫描记进 `state.qbit_errors`，走第 1 阶段同一条路：执行器整批拒绝、`run` 退出码 3、stdout 与 stderr
各说一遍（带着怎么确认的命令）。生产 539 个种子时阈值是 54 个。

- **基线** `state/health/torrent-count.json` = 最近一次**被采信**的 `run` 的计数与扫描之前那一刻的时间。只有
  `run` 写、只在扫描完整时写：被拒绝的一轮不挪基线——否则第二轮就拿残缺的数当基准放行了（有测试）。
- **审计里的摘除**（`torrent_removals`）：基线时刻（含）之后、非预演、按 hash 去重，认生产审计里摘掉种子记录的
  每一种形状——`drop_torrent` applied、整种子隔离（`undo.torrent_record_lost`）、幻影只摘记录（`dropped`）、
  搬文件失败但种子已摘（`torrent_record_lost`）、未确认但发出过 `qbit.delete`。回退加回来的不减（宽松一侧）。
  本项目一轮摘掉 25 个死种，下一轮 30 → 5 照常放行（真流程测过）。
- **人在 qBit 里手动批量删**：审计里没有，会一直拦到有人执行 `media-agent health --accept-torrent-count`（按此刻
  qBittorrent 的数重置基线，`source=accepted`；qBit 不可用就拒绝、基线不动）。取舍：宁可停几轮（每轮都大声说、
  退出码 3、健康报告与通知都会报），也不拿残缺视图动手；没有做"连续几轮都是这个数就自动认"——一个一直只恢复
  了一部分的会话同样会连续几轮稳定。
- 基线文件坏了 / 不存在（第一次部署）当作没有基线，这一轮之后记上。

**测试**：`tests/test_torrent_count.py`——第一轮记基线；30 → 5 且审计里没有摘除：拒绝、一个字节不改、基线不动，
第二轮同样拒绝；本项目自己摘 25 个死种后 30 → 5 放行；阈值内放行；539 的 10% 口径；审计里摘除的五种形状与
四种不算的；确认命令重置基线、没 qBit 时拒绝；坏基线文件；阈值配置校验。关掉扫描里的检查，前两个红；
不减审计里的摘除，死种那个红。

## 6. 每轮健康报告（`health.RunHealth`，`media-agent health`）

**现场**：`run` 的输出只有发现清单与一行"执行 N 项"；退出码只在"整批拒绝"（3）与"审计没写全"（4）时非零，
异常冲出就是一段 traceback。抓取连着十天 NameError、检测器崩了、磁盘快满——launchd 上都是 last exit code 0。

**做法**：`cmd_run` 拆成 `_run`（原来的主体）与收尾 `_finish_run`：

- `RunHealth` 边跑边记：客户端状况（`build_context` 填进 `ctx.client_status`：`ok` / `down: 原因` / `off（为什么）`；
  测试里直接构造的 Context 按客户端在不在推断）、扫描（`qbit_errors`、此刻的种子数——`LibraryState.qbit_listed`
  分得清"0 个"与"没读到"——与上一轮的基线）、检测器（条数、`Registry.errors`）、发现（按严重度）、执行报告
  （四种状态的条数、failed / unknown 前 10 条、审计转写）、抓取统计（提议 / 加上 / 409 已存在 / 元数据超时 /
  失败 / 未确认 / 跳过）、发布名文件超过 `UNRENAMED_ALERT_HOURS`（默认 12）还没改的（`unrenamed-file` 提了、这一轮
  没改成；有种子按加入时间、本地文件按 ctime）、隔离区（总大小 / 文件数、剩余空间、容量闸、处置结果）、卡住、
  演进、`ctx.log` 里"失败 / 出错"的行数（按 `[标签]` 归类，`tap` 包在 `ctx.log` 外面）、耗时。
- **try / finally 语义**：`_run` 正常返回、整批拒绝、异常冲出（`except Exception`：完整 traceback 进 stderr，摘要
  `{error, where}` 进报告，退出码 1——与 Python 未捕获异常相同）、甚至 Ctrl-C（`BaseException`：报告照写，异常照抛）
  都会写 `state/health/<批次 ID>.json`（只留 60 份）并在输出末尾打印一小节。没跑到的阶段在报告里是 null——"没跑到"，
  不是"没问题"。收尾自己出错只在 stderr 说一句，不改变这一轮的退出码。
- **状态**（`RunHealth.reasons`，每条原因带 `level` / `code` / `text`）：
  - critical：`crash`（1）、`refused` 整批拒绝（3）、`rescan_degraded` 演进重扫时读不全（3）、`audit_incomplete`（4）、
    `disk_full` 处置之后媒体卷估计剩余仍低于 `MIN_FREE_GB`（**5，新**）；
  - warn：`paused`（第 9 节）、`detector_crash`、`failed_actions`、`unknown_actions`、`stuck`（未确认的）、
    `unrenamed_old`、`ab_down`、`tmdb_off`、`low_space`（低过阈值但提前删回来了）、`free_space_unknown`、
    `disposal_failed`，以及 `ab_container_maybe_stopped`（复审时补：改 AB 数据库之后 `docker start` 报了错、库已改好
    ——`_ab_write` 记 applied 带 `after_error_note`。以前只有一行日志与审计里的一个字段，这一轮 ok、不发信；客户端状况是
    在写库之前取的，最早要 6 小时后下一轮 AB 登录失败才看得见。报告的 `actions.ab_maybe_stopped` 列出是哪几条）；
  - 其余 ok。"日志里有报错行"只列出、不改状态：生产上 TMDB 查询这类一次性失败每轮都可能有，拿它定 warn 会让
    warn 变成常态、没人再看。
- **退出码** = `exit_code_for(critical 原因, _run 的返回值)`：几种同时出现时取 1 > 3 > 4 > 5。warn 一律 0。launchd 的
  `StartInterval` 任务只记下退出码、6 小时后照常起下一轮（plist 里没有 `KeepAlive`，非零不会触发重启节流），
  所以 5 只是让 `launchctl list` 看得见。
- `media-agent health [--run ID] [--json]`：最近一轮（或指定那一轮）的报告；没有就退出码 1。
- **反复出现的失败**（`health.repeated_failures`，复审时补）：这一轮的 failed / unknown 里，同一个规则、动作、错误（前 60 字）
  在最近 14 天的至少两个批次里出现过的（`evolution.find_failure_patterns`，以前只在 `evolve` 里打印；runloop §8b 把它列为
  健康摘要该收的信号），写进 `actions.repeated`，`failed_actions` / `unknown_actions` 的原因里点名。每轮抓的是不同的集、指纹
  各不相同时卡住检测连不起来——2026-09-16 … 09-26 的 12 次抓取 NameError 就是这种。

**测试**：`tests/test_health_report.py`——干净的一轮 ok、与发现历史 / 审计同一个批次 ID；第二轮带上一轮的种子数；
整批拒绝 critical / 3；`disposal.dispose` 抛异常时报告照写、critical / 1、traceback 进 stderr；审计写不进去 critical / 4；
处置之后仍低于阈值 critical / 5（`test_quarantine_disposal` 里原来断言 0 的那条随之改成 5）；退出码优先级；检测器崩溃、
动作失败、TMDB 没配、未改名超时、卡住各自 warn，确认之后回到 ok；这一轮改掉的不算未改名；抓取统计；隔离区大小
与处置；`health` 命令的最近 / 指定 / JSON / 不存在；报告目录写不进去不改退出码；只留 60 份；配置校验。

## 7. 被吞掉的异常：逐个说出来，或写明为什么不说

**现场**：`media_agent/` 里 83 处 `except Exception` / 裸 `except`。大多数吞得有理由（单个请求、单条规则出错不能拖垮
整轮），而且第 1、2 阶段与本阶段前几节已经让其中多数进了审计（`_settle` / `_audit` / `_crashed`）、往上抛
（`ClaimsUnknown`）或记进错误列表。剩下 25 处吞得不声不响——`bug-fix/2026-08-30-silent-failures-sweep.md` 那五个
"不报错、只是不干活"的 bug 就是这种形态。

**做法**（行为一律不变，只是说出来）：

- 18 处改成 `ctx.log` 一行、带上下文（哪个规则 / 哪部番 / 哪个种子 / 异常），前缀 `[规则 id]` 或 `[scan]` /
  `[rollback]` / `[evolve]` / `[grab]`，统一用「失败」——健康报告（第 6 节）按 `[标签]` 数它们，测试的 tripwire
  （`log_failure`）也认它。涉及：scan 里 TMDB 取标题 / 季信息；incomplete-season、source-abandoned、episode-available
  里 TMDB 集表、RSS、Mikan 搜索 / 字幕组 / 候选 feed（`_resolve_mikan_id` 为此多了 `log` 参数）；dead-torrent 与
  stale-torrent-path 读 `files()`；抓取后改种子显示名（没改成的话 AB 会按分季集号改回去，2026-08-31 Re:Zero 那次拉锯）；
  回退 relink 时改不回条目名 / save_path（以前一声不吭、照报"已还原"——仍算还原，见遗留）；演进影子验证里别的规则崩了。
- 演进规则文件读不了 / 格式不对：`load_rule_specs(errors=…)` 把原因交给调用方，`Registry.load_errors` 记下，
  `build_registry` 在 stderr 说、健康报告 warn（`rule_load_failed`）。以前坏文件一声不吭地少挂一条（evolution 调研 §10b）。
- 7 处写明为什么不说：AB 就绪轮询、`.torrent` 解析不了（调用方按"说不清"处理并写进审计）、relink 触发校验出错
  （写进那条 applied 记录的 `recheck_error`）、目录改名 setLocation 出错（记进 `move_failed / move_unsure`、随后写审计）、
  AniList 退避重试（没有调用方，critic N20）、DSL 条件求值（逐文件 × 逐规则，报了会刷屏；坏规则在加载时报）、
  `_season_offsets`（`sidecar.load` 自己兜了坏 JSON；剩下的"没有偏移"是安全一侧）。
- `tests/test_silent_excepts.py` 按 AST 检查：每个宽 `except` 要么调用日志 / 审计 / 汇报（`log`、`_audit`、`_settle`、
  `on_error`、`errors.append`……），要么 `raise`、要么把异常交给调用方（`return f"…{e}"`），否则那一行必须有注释。
  以后再加一个不声不响的，这个测试红。**只写 `# noqa: BLE001` 不算注释**（复审时补）：ruff 的 BLE001 提示的正是这一句，
  顺手压掉告警的那一下以前同时满足了这个检查（变异 X29）。现在去掉 `noqa…` 之后还得有字；`_describe`、`_confirm`、
  `audit.write` 三处只有 `noqa` 的补上了一句为什么。

**遗留**：回退 relink 时条目改不回来仍算"已还原"（现在会说出来）；`sidecar.load` 把坏 JSON 当作空 sidecar
（`require_any` / `season_offsets` 这些用户意图就此消失）是窄 except，不在这次范围里；source-abandoned 取集表失败时
缓存了空结果（原有行为，现在会说出来）。

**测试**：`tests/test_silent_excepts.py`（检查器本身与它的对照）、`tests/test_silent_excepts_report.py`——TMDB 取标题
失败说出来、番照旧按没匹配处理；回退 relink 改不回条目说出来、照旧算还原；坏规则文件报出来、其余照常挂上、
`build_registry` 在 stderr 说；健康报告数到这些行（`logged_errors`）。`test_dead_torrent` 里注入 `files()` 超时的那条
测试现在声明了预期的那一行日志。改之前这 7 个测试里 6 个红。

## 8. 通知邮件（`media_agent/notify.py`）

**为什么**：健康报告写在生产机的 `state/health/` 里，没人去看就等于没写。可每轮都发一封，第三天就没人看邮件了。

**只发变化，一轮最多一封**（`notify.events`）：状态变坏（ok → warn、ok / warn → critical，第一轮就不是 ok 也算）；
从 critical 出来（→ ok / warn；warn → ok 不发，没什么要人做的）；新出现的卡住指纹（状态一直是 warn 也照发，消失后再
出现再发）；新进入整批拒绝、审计开始转写（持续期间只发第一轮）。去重状态 `state/notify.json`：`last_status` 是**人最后
一次被告知的**状态，`active` 是正在持续的事件（`stuck:<指纹>` / `degraded` / `audit_fallback` → 第一次发出的时间）。

**"消失"只认评估过的轮次**（复审时改的）：被锁挡住、维护暂停、整批拒绝、半路崩溃的一轮走不到卡住检测（报告里
`stuck` 是 null），暂停与被锁挡住的也走不到执行器（`actions` 是 null）。以前 `active` 每轮从零重建，这样一轮把
`stuck:<指纹>` 全清了——下一轮正常的 run 把每个一直卡着的指纹当「新卡住」再发一遍，与"同一个指纹只发一次"相悖；
卡住检测自己的连续段并没有断（这些轮次不写快照或写降级快照）。每次 qBit 超时、部署撞上一轮、看门狗重建容器、救援
暂停都会触发；按生产 run.log 回放，8 月底同时卡着 58–66 个，一次打断就是一封重列几十条的信。现在：`stuck` 为 null 时
`stuck:*` 原样带着；没走到执行器时 `degraded`、`audit_fallback` 原样带着（整批拒绝的一轮照常评估 `degraded`）。

**发不出去**：stderr 说一句、`failures` 加一、写进这一轮健康报告的 `notify`；这一轮的事件存进 `notify.json` 的
`undelivered`（带批次与时间），下一封信开头补上「（补发：批次 X，时间）…」，发出去才清空。最多留 50 条，更早的只计数
（`undelivered_dropped`，补发时说一句）。永远不拦这一轮、不改退出码。任何意外同样兜住。

最初的做法是**不更新** `last_status` 与 `active`、下一轮按人最后一次被告知的状态重算——复审时发现它会丢事件：
ok → critical（整批拒绝）那封没发出去，下一轮已经回到 ok，重算的结果是"没有变化"，状态静静推进，这次拒绝除了生产机上
那份健康报告谁也不知道。现在去重状态照常推进（它表示"已告知或已排队"），补发的与这一轮新算的不会重复；信里按时间
先后是「补发：ok → critical、新进入整批拒绝」再「critical → ok」。主题的"恢复"只看这一轮自己的状态事件。

**永不带密钥**（`notify.redact`）：配置里的 qBit / AB 密码、TMDB key、LLM key、SMTP 密码原样换成 `***`；URL 里的
`api_key=` / `token=` / `password=` 等、`Bearer …`、`scheme://user:pass@` 也遮掉——httpx 的报错会带上整个请求 URL，
TMDB 的 `api_key` 就在查询串里。健康报告落盘前同样过一遍（`redact_obj`）。配置里的密钥只在 ≥ 8 个字符时按原文
替换：测试里 `qbit_pass="test"` 把健康报告里路径的 `pytest-…` / `test_…` 也换成了 `***`，路径就对不上了——短密钥
只会随 URL / 请求头出现，由上面的模式遮掉。

**信**：主题 `[media-agent] <图标> <状态>：<第一条原因>`（恢复时写"恢复"），正文是事件列表 + 健康报告的文字版
（`health.render`）+ `media-agent health --run …`；另附一小段 HTML（事件列表 + `<pre>`）。SMTP over SSL
（`smtplib.SMTP_SSL`，默认 465，`ssl.create_default_context()`），配了用户就登录。发件人是 `NOTIFY_SMTP_USER`。

**配置**：`NOTIFY_EMAIL_TO` / `NOTIFY_SMTP_HOST` / `NOTIFY_SMTP_PORT=465` / `NOTIFY_SMTP_USER` / `NOTIFY_SMTP_PASS`；
收件人与主机都没配 = 关闭、不写 `notify.json`、一个字不说；只配了一半 = 关闭并在 stderr 提醒；端口写错启动即报错。

**测试**：`tests/test_notify.py`（`smtplib.SMTP_SSL` 换成替身，不联网）——健康的第一轮不发；ok → critical 发一封、
再拒绝不再发；从 critical 恢复发；warn → ok 不发；新卡住发一次、状态一直 warn 时第二个新卡住照发；密钥（配置里的与
URL / Bearer / userinfo 里的）不进信也不进健康报告；发送失败说出来、计数、下一轮重发；没配置一声不吭；配一半提醒；
配置默认值与校验；`redact` 本身。没评估的轮次：卡住 → 被锁挡住 → 卡住不再发；评估过、解决了再出现照发；拒绝 → 暂停 →
拒绝只报一次；审计转写穿过被锁挡住的一轮；端到端卡住 → 整批拒绝 → 暂停 → 被锁挡住 → 正常，「新卡住」只有一封（改之前
这 5 个里 4 个红）。发送失败的 critical 下一轮已回到 ok 照样补发（改之前红）；补发上限 50 条、其余计数。

## 9. run 的日志：每行带时间与批次 ID，进程里轮转（`media_agent/runlog.py`）

**现场**（runloop §4）：launchd 把 `run` 的 stdout / stderr 追加到 `state/run.log` / `run.err.log`。生产 run.log
1.8 MB / 159 轮：没有时间戳、没有批次号、没有轮次分隔、从不轮转。

**每行带前缀**：`main` 对 `run` 先定批次 ID（`args.run_id`，`cmd_run` 用它——发现历史、审计、处置、健康报告与日志
前缀是同一个），把 `sys.stdout` / `sys.stderr` 换成 `runlog.Stamped`：按整行攒，每行前面加
`2026-09-26T12:00:00 [批次 ID] `（`print` 常分两次写，多行字符串一次写好几行，按 `write` 调用加前缀会错位）；结束时
（含异常、`traceback.print_exc`）把没换行的尾巴也带上前缀、换回原来的流。包住的是拿锁、轮转、执行、健康报告的全程，
所以"被锁挡住"（75）与维护暂停的那一行也带前缀。拿到锁之后先打一行
`═══ media-agent <版本> run 开始：批次 … ═══` 作轮次分隔。只包 `run`：人手动跑的命令不加。

**轮转：先拷贝、再截断，不改名**。launchd 每轮启动时按路径打开 run.log、把描述符交给进程；进程里改名，本进程的 1 号
描述符仍指着那个 inode——这一轮的输出全进了 `.1`，新的 run.log 要到下一轮才出现（测试里有这个对照）。所以
`.4 → .5 … .1 → .2`（留 5 代）→ 整份拷成 `.1`（临时文件再改名；拷不出去就不截断，一行不丢）→ 截断成 0 →
本进程指着它的描述符 `lseek` 到尾。最后一步是因为截断之后写到哪取决于打开方式：带 `O_APPEND` 的落在文件尾——
开源 launchd（apple-oss-distributions/launchd `src/core.c` 5189–5190）就是 `O_WRONLY|O_CREAT|O_APPEND`；不带的偏移
停在旧文件尾，下一次写会留下几 MB 的 NUL 空洞。如今的 launchd 不开源，不押在它上面。轮转在拿到运行锁之后、打印
任何东西之前（锁保证没有别的 media-agent 在写）；阈值 5 MB。

**测试**：`tests/test_runlog.py`——整行 / 半行 / 多行字符串 / 空行 / 没换行的尾巴都带前缀；异常时换回原来的流；
`main run` 的输出带它自己的批次 ID、第一行是轮次分隔，别的命令不加；轮转在 `O_APPEND` 与不带两种描述符下都让之后的写
落在新的 run.log 开头（去掉 `lseek`，后者红）；改名式轮转的对照；5 代、最老的丢掉；5 MB 以内不动；两个文件各自
判断；拷不出去不截断；`main run` 在打印之前轮转并说一句。

## 10. 维护暂停（`media_agent/pause.py`）

**现场**（critic N17）：`rescue.py` 与 `vpn-watchdog.sh` 会重建 qBittorrent 容器；救援期间做种被暂停、分享率上限改成
1.0、流量走按量计费的 VPS。media-agent 什么都不看：一轮 run 可以正好落在重建中途，也可以在救援期间摘种子、抓新的。

**做法**：

- 暂停条件：救援标记 `~/gluetun/.rescue-active`（`deploy/rescue.py` 的 `MARKER = DIR / ".rescue-active"`，
  `vpn-watchdog.sh` 避让看的也是它；`RESCUE_MARKER` 可改），或 `state/PAUSE`（人手动放的，里面可写一句为什么）。
- 声明 `pause=True` 的子命令（`run`、`apply`，以后的抓取模式同样声明）一开始就以 **75** 结束——与"运行锁被占"同一个
  退出码（EX_TEMPFAIL，"被挡住了，下轮再来"）——stdout 与 stderr 各说一遍：为什么暂停、已经多久、怎么恢复。
  `diagnose` / `scan` / `health` / `runs` 照常；人手动的 `rollback` / `repair` / `purge` / `evolve` 不拦（维护期间正是人在
  操作）。
- **不拿运行锁就返回**：救援脚本重建容器时自己拿着那把锁（第 11 节），暂停的 run 不去等那 10 秒。
- 暂停的 `run` 照样写健康报告（warn，原因 `paused`，退出码 75），ok → warn 发一封通知：一个忘了删的 `state/PAUSE`
  不能让 agent 悄悄停摆——这一阶段要解决的正是这种停摆。
- 同理，**被运行锁挡住的 `run`**（另一个进程拿着 `state/run.lock`，等满 10 秒）也写 warn 报告（原因 `locked`，带持有者
  的自述：pid、命令、从什么时候起），退出码仍是 75。以前只有一行输出：锁若被一个卡死的进程一直拿着，每一轮都这样
  悄悄结束。与部署偶尔撞上一轮会因此多一封 ok → warn 的通知，下一轮恢复 ok 不再发。
- 直接构造的 `Config`（测试基座）`rescue_marker=None` = 不看；conftest 另把 `RESCUE_MARKER` 指到临时目录——开发机上
  真有 `~/gluetun` 时测试也不会被暂停。

**测试**：`tests/test_pause.py`——救援标记让 `run` / `apply` 以 75 结束、说清原因；`state/PAUSE` 的内容与怎么恢复；
`diagnose` 照常；不暂停照常；暂停的 run 不等锁；暂停的 run 写 warn 健康报告；时长；默认路径；测试基座不看；哪些子命令
声明了暂停。把 `run` 的 `pause=True` 去掉，5 个红。被锁挡住的 run 写 warn 报告、带持有者。

## 11. 救援脚本与看门狗重建容器时拿运行锁（`deploy/rescue.py`、`deploy/vpn-watchdog.sh`）

**现场**（critic N17）：两个脚本都 `docker compose up -d --force-recreate`，qBittorrent（与 gluetun 共用网络命名空间）
跟着被拆掉重建，不看任何锁。第 1 阶段"读不全就整批拒绝"只挡得住扫描那一刻；重建落在执行阶段中途，改名 / 隔离
就停在半路。

**做法**：

- `rescue.py`：`start` / `stop` 的主体包在 `media_agent_held()` 里——`media_agent.runlock.RunLock` 拿
  `$MEDIA_AGENT_HOME/state/run.lock`，最多等 `RESCUE_LOCK_WAIT`（默认 900）秒，等不到就不切换、以 75 结束、打印持有者。
  同一个进程里重入放行：`start` 的 compose 失败时它在锁里调用 `stop` 回滚，不能自己把自己锁死。`auto` 等下载的几个小时
  不拿锁（那期间救援标记让 media-agent 自己暂停，第 10 节）；`stop` 等不到锁时标记留着——安全一侧，人稍后再跑一次。
- `vpn-watchdog.sh`：判定 BROKEN 之后、重建之前，`with_runlock`（macOS `/usr/bin/lockf -k -s -t`，Linux `flock -w`，
  都没有退回 python3 的 `fcntl.flock`；与 `deploy.sh` 的 `with_lock` 同一种锁、锁文件永不删除）在锁里带
  `MA_RUNLOCK_HELD=1` 把自己重跑一遍——等锁的几分钟里隧道可能自己好了，重跑先重新看健康状态。等不到退出码 75、
  日志记 `SKIP` 与持有者；media-agent 的 state 目录不在就记 `WARN`、不拿锁直接重建（隧道要紧）。顺带：docker 路径读
  `DOCKER_BIN`（deploy/README 的配置表一直这么写，脚本却写死了），重建后查健康的间隔读 `WATCHDOG_POLL`（测试用 0）。
- 等锁上限 900 秒：一轮 run 约 2 分钟（runloop §6），deploy.sh 的 `DEPLOY_LOCK_WAIT` 也是 900。
- **生产上跑的是 `~/gluetun/` 下的拷贝**，`deploy.sh` 不碰它们：deploy/README「生产上的副本要手动同步」写了 diff → cp →
  `bash -n` 的步骤。本阶段没有动生产。

**测试**：`tests/test_rescue_scripts_lock.py`——`rescue.py`（按文件导入，docker / qBit 换成替身）：start 只在拿着锁时重建、
用完即放；media-agent 占着锁时 75、没暂停做种、没写标记、说清持有者；stop 在锁里重建并删标记；start 失败回滚时同一个
锁里重入。`vpn-watchdog.sh`（真 `/bin/bash` 跑，替身 docker 在 compose 那一刻用 `flock(LOCK_NB)` 看锁有没有被拿着）：
重建时锁被拿着、恢复；media-agent 占着锁时 75、没重建、日志记 SKIP 与持有者；隧道健康时什么都不做；读 `DOCKER_BIN`。
`test_deploy_scripts` 的 bash 3.2 解析与"变量名后紧跟中文"检查照样覆盖 watchdog。
