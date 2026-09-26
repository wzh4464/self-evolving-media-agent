# 一轮之内收敛：扫描 → 诊断 → 执行，重复到不动点

**日期**: 2026-09-27
**状态**: implemented / architecture
**触发**: 整改第 4 阶段 F1–F4。runloop 调研 §7A / §8a、critic §3.2（先保证快照新鲜）与 §3.12（一个执行器、反向闸、
不重试的备忘）。

## 为什么

一轮 `run` 以前是"先全量诊断、再统一执行"：执行里做成的事，要等**下一轮**（6 小时后）的诊断才看得见。前后依赖的
两步因此永远隔一轮：

- **AutoBangumi 的重复版本落在 `Bangumi` 分类**：这一轮只能交接分类（recategorize）；判重要等交接完才敢做（所有权
  边界，AGENTS.md 第 6 条）；赢家的改名要等判重腾出集位——第一轮改名撞「集位被占」。生产上最近 3 批没有隔离却有
  「集位被占」的，同一批里都有一次 recategorize。
- **抓取之后的改名**：元数据没在等待时限内到、或种子不止一个视频，改名"下一轮会补"（2026-09-03 最新三集刮削失败：
  文件在发布名上躺了六小时）。
- relink（op 1）、目录改名（op 8）之后，下游规则看到的都是改之前的样子。

## 做法（`media_agent/converge.py`）

`converge.run(ctx, reg, ex, scan=…, max_iterations=…, select=…)`：只要上一次迭代**做成了新的动作**就再扫描、诊断、
执行一次，最多 `max_iterations` 次。`cmd_run` 怎么用它见下一节。

- **一轮一个执行器**：一个批次 ID（整轮仍是一个回退单元）、删除配额跨迭代累计（每次迭代新建执行器，
  `MAX_DELETE_PER_RUN=50` 就成了 N×50）、`_grabbed`（写 sidecar 时并回本轮抓的集）、本轮摘掉的种子（按 hash：
  qBittorrent 的删除是异步的，占用索引要继续无视它们）。`Executor.new_iteration()` 在每次迭代之前清掉占用索引与
  **按路径**记的"本批次已隔离"：新的扫描已经反映了隔离；同一个路径上此刻可能是改名过来的赢家，还按路径记着，删除
  关口会说「保留方 … 本批次已被移进隔离区」——假的拒绝（`test_a_path_trashed_in_one_iteration_can_hold_the_keeper_in_the_next`
  不清就红）。
- **每次迭代一份新扫描**：`build_state` 开头清种子文件列表与 `season_offsets` 的进程内缓存（testinfra B3，第 1 阶段已修）；
  `probe._CACHE` 按 (路径, 大小, mtime) 认，改名后的文件自然不命中。TMDB 身份照常解析——不能像演进重扫那样
  `resolve_tmdb=False`（标题退回目录名，同一轮里来回改名，runloop B4）。
- **不重试**：一个动作按"做什么 + 对谁"认（`key_of`：改名 / 隔离按路径 + 种子，分类 / 标签 / 摘种按 hash，写档案按
  番目录，抓取按番目录 + 季 + 集，不认识的按全部参数）。已执行、失败、未确认、被闸拦下（删除关口、配额、演进规则、
  坏 sidecar、目标目录要人合并、种子已不在……）的，后面的迭代不再试——否则每次迭代都会再撞一次同样的 404（testinfra B2）、同样的
  拒绝，多写一条审计。只有**此刻被别的东西挡着、而本轮别的动作可能把它挪开**的跳过（`RETRYABLE`：「集位被占」、
  relink 的「目标文件已归另一个活种子」）后面的迭代再试：判重在下一次迭代腾出集位，赢家就改得成。这类跳过最多每次
  迭代一条（上限 3）。
- **反向动作**：一个动作要是会撤销本轮已经执行的动作——按那条记录自己的逆操作认（`undoes`）：改名 X→Y 之后 Y→X、
  目录改名来回、分类 A→B→A、relink 的映射反过来、刚抓的种子又要摘 / 隔离、刚摘的种子又要抓回来——拒绝它，记一条
  skipped 审计（「反向动作：…」，带 `reverses`），报一条 `run-loop` / `oscillation` 发现（important）。那是两条规则在
  打架；runloop B5 / LAT-04（鬼物语 20260919 两轮之间改名又改回）压进一轮就是这个形状。同一个反向动作一轮只报一次。
- **刚动过的种子这一轮不按死种摘**（critic §3.3）：relink（renameFile + recheck）、relocate / 目录改名（setLocation）之后，
  种子有一阵子是"下载中、0 做种、0 可用"——刚校验完 / 刚搬完，还没连上 peer。死种判定按最后一次活动算停滞时长
  （3ef6825），对一个几个月前加的老种子，这一刻就够"死"了。以前一轮只诊断一次，中间隔着 6 小时；迭代时下一次迭代就会
  把它摘掉。`TOUCHING` 里的动作做过的种子，这一轮的 `drop_torrent`（`dead`）记 skipped「刚动过的种子：…」（带
  `touched_by`），下一轮照常判（`test_a_torrent_relinked_this_run_is_not_dropped_as_dead_in_the_same_run`：recheck 只对上
  一半分片，改之前同一轮就摘了）。
- **先等 qBittorrent 搬完再扫描**（2026-09-27 审查）：qBittorrent 5.2.3 的 setLocation 是异步的（`Executor._location_landed`），
  以前下一次迭代在 `apply` 返回的那一刻就扫描——目录改名之后种子还在旧目录里搬，新目录里只有 `_merge_tree` 挪过去的
  纯本地文件、NFO 与档案，扫描把两个目录当成两部番：sidecar-sync 按半搬的视图把新目录的 `have` 从 [1, 2, 3] 写成 [3]；
  第一次迭代给旧目录写的档案（旧目录还在，因为种子还在搬）搬空之后留下一个只剩 `.media-agent.json` 的幽灵目录
  （`FakeQbit.async_moves` 复现）。现在上一次迭代做过 `MOVES`（`rename_show_dir`、`relocate`，含半途而止的）的种子，下一次
  扫描（含到顶后的收尾诊断）之前先等它们不再是 `moving`，最多 `SETTLE_TIMEOUT_S`（60 秒，每秒看一次）；等不到或读不到
  qBittorrent 就停在这里（`stop = moving`，原因在 `unsettled`），这一轮剩下的下一轮做——不是 warn：同卷搬运是改名，
  一两秒就完。另外执行器记下本批次改了名的目录（`Executor._renamed_dirs`，跨迭代），写档案 / 钉身份不再写进旧目录，
  只看 `is_dir()` 挡不住还在搬的旧目录。
- **停在哪**：某次迭代没做成任何新动作 = 不动点（`fixed_point`）。到了上限还在做新动作：再扫描、诊断一次（**不执行**、
  不写审计），下一次迭代会做的列为"待做"（`cap`）；一件都没有就仍算不动点。任何一次迭代的扫描读 qBittorrent 不完整，
  执行器照旧整批拒绝（`qbit_blocker`），循环立刻停（`refused`）。预演只跑一次（`dry_run`）：动作都没执行，第二次看到的
  与第一次相同。
- **结果**（`Outcome`）：`findings` 是**最后一次**诊断的（到顶时是收尾诊断的）加上本轮的 `oscillation`；`iterations`
  是每次迭代的扫描 / 诊断 / 执行耗时与计数；`proposed` 是全部迭代里提过的动作（按目标去重），`title_decisions` 是各次
  迭代的标题决定（调用方一轮只记一次）、`detector_errors` 是各次迭代崩过的规则。
- **模型选的身份这一轮不用，迭代也一样**：`pin_tmdb` 把模型在多个 TMDB 候选里选的条目钉进 sidecar，约定是"这一轮不用、
  下一轮起照它认"（critic N4，[身份钉住](2026-09-26-tmdb-identity-pinning.md)）。迭代时第二次扫描已经照 sidecar 认它了——
  离线复现：同一轮里接着写 NFO、改文件名、改目录名，人连看一眼 `tmdb_pick` 的机会都没有。所以后面的迭代里这些番挂
  `naming_hold`（`converge.HOLD_FRESH_PIN`），改名、目录名、分类、NFO、抓取都不按它做，`tmdb-identity` 报 `naming_held`；
  下一轮照常。
- **目录改了名，身份跟着目录走**（2026-09-27 审查）：没钉 tmdb_id 的番，第一次迭代按旧目录名搜到条目 A、按它写 NFO、
  把目录改成 TMDB 标题（op 8）；写 sidecar（op 10）因为目录已不在被跳过，身份没钉进去。第二次迭代按**新目录名**（就是
  TMDB 标题）重新搜，同名的另一个条目 B（重制版、真人版）让它选了 B——没开模型取第一个、sidecar-sync 钉成 `search`；开了
  问模型、`pin_tmdb` 钉成 `llm`。此后只有人改得了，NFO 与目录名却是按 A 写的；B 是完结的老条目时抓取还会悄悄停。
  现在 `rename_show_dir` 做成之后按新目录名记一条只有 id 的旧格式缓存（`Executor._carry_identity`，`scan._search_tmdb`
  最先查它），下一次扫描（迭代或下一轮）照 A 认、sidecar-sync 把 A 钉进去（`test_a_renamed_dir_keeps_the_identity_it_was_renamed_by`：
  改之前第二次迭代 `search_tv('同名')` 选了 88，开模型时又问了一次模型）。只记 id：带标题的旧格式条目会被 `_tmdb_meta`
  当元数据迁过去，按 id 的缓存过期后它又顶 30 天，TMDB 改了的标题迟迟看不见。

## `media-agent run`（`cli._run`）

- `MAX_ITERATIONS`（新配置，默认 3，至少 1；1 = 以前的单次，外加一次不执行的收尾诊断列出待做）。
- **一轮只做一次的，按最后一次诊断做**：发现历史（`state/findings/<批次 ID>.jsonl`）与卡住检测看这一轮**结束时**还在的
  问题——已经在这一轮修好的不算"还在"；健康报告的"发现"一节同样按最后一次诊断，"执行"一节是各次迭代的合计，抓取的
  "提议"按各次迭代提过的合起来，检测器崩溃按各次迭代合起来。
- **TMDB 标题稳定闸一轮只记一次**（`titles.record`，各次迭代的决定合起来）：迭代之间记了，同一轮的第二次迭代就会把新标题
  "确认"下来——一次 TMDB 抖动就改名，LAT-04 又回来了（`test_a_run_that_iterates_still_counts_as_one_run_for_the_title_gate`）。
- 种子数基线、出处账本的增量补录只在第一次迭代的扫描之后做：本轮抓的执行器当场记账，扫描每次都重读账本；后面的迭代
  少了种子由审计里的摘除解释（`health.torrent_count_problem`）。
- **读不全**：第一次迭代读不全与以前一样（整批拒绝、退出码 3、不演进、不处置隔离区）。第二次迭代起读不全，同样按拒绝收尾，
  原因写明「第 N 次迭代：…；此前 N−1 次迭代已执行 K 项（批次 …，可 rollback）」，前面迭代做的照样进健康报告的"执行"一节，
  `degraded.qbit_errors` 是读不全的那一次的。到顶之后的收尾诊断读不全：修复已经做了、处置照跑，按 `rescan_degraded`
  报 critical、退出码 3（与演进重扫读不全同一个口径）。
- **半路冲出**（后面迭代的扫描抛异常、Ctrl-C）：`cmd_run` 照旧记 crash、退出码 1；前面迭代已经做了的照样进健康报告
  （`loop` 一节停在 `crashed`、`actions` 一节是那几次迭代的合计）——以前异常之前的阶段报告里都有，迭代之后这一节会变成
  null（"没跑到"），而它跑了。`converge.run(out=…)` 让调用方拿得到半截的结果。
- **输出**：第一次迭代打印完整的发现清单（与以前一样），之后每次迭代一行（扫描 / 诊断耗时、几个问题、可执行几个、执行 /
  跳过 / 失败 / 未确认、本轮已试过、反向拒绝），最后一行「修复：执行 N 项…（K 次迭代，不动点 / 到上限）」；到顶时逐条列出
  待做，打架的逐条说。健康一节多一行「迭代 K 次 · 不动点（每次执行 3 / 2 / 0）」。健康报告新增 warn `loop_cap`、`oscillation`。
- **演进不动**（冻结着）：`EVOLVE_MODE=propose` 时仍在迭代之后单独重扫一次（`resolve_tmdb=False`，runloop B4 那一行原样
  留着），只是它现在排在不动点之后。

## 每次迭代跑哪些检测器

**全部**，不按迭代挑：挑了的话"最后一次诊断"就成了几次迭代拼起来的，发现历史与卡住检测没法信。能这样做是因为第二次
迭代起网络全走缓存——TMDB 身份与标题按 id 30 天、搜不到的负缓存 24 小时、分集表 6 小时 / 7 天
（`cache.season_episodes`，见 [分集表缓存](../bug-fix/2026-09-27-season-episodes-cache.md)）、番组页与 RSS 1 小时、Mikan
搜索 7 天——而本地动作改变不了其中任何一样。剩下的是 qBittorrent 的种子文件列表（必须是此刻的）与磁盘。TMDB 挂着时的两个断路器
（扫描的 `ctx.tmdb_scan_down`、分集表的 `ctx.tmdb_episodes_down`）都跟着 Context 走——一轮 `run` 只等一次超时，不是每次迭代
各等一次（`test_a_tmdb_outage_costs_one_timeout_per_run_not_per_iteration`：改之前 3 次迭代打了 3 次）。番组页 / RSS / Mikan
搜索拉不到的网址同理：检测器的网页请求都经 `subscription._fetch`，失败的网址 30 分钟内（`FETCH_FAIL_TTL`，进程级——launchd
每轮一个新进程）不再请求、直接报"刚失败过"，调用方照旧说出来、跳过（`test_a_mikan_outage_costs_one_timeout_per_url_per_run`：
改之前 3 次迭代请求了 3 次；Mikan 挂着时约 35 条订阅 × 25 秒超时 × 每次迭代）。
`test_iterations_after_the_first_diagnose_without_any_network_call` 钉住：第二次迭代起扫描 + 诊断的 TMDB / 网页 / 模型调用都是 0。
例外：这一轮刚 `pin_tmdb` 钉进去的条目，第二次迭代照 sidecar 认它时按 id 取一次标题与季（模型选的那一刻只有搜索结果），
分集表同理——一个新钉的条目一轮一两次请求，之后走缓存。

离线基座按生产规模量（129 部番、159 个季键、1833 个种子、TMDB 0.05 秒一次，一处 AB 重复版本让这一轮要两次迭代）：

| 迭代 | 扫描 | 诊断 | 执行 |
|---|---|---|---|
| 1 | 7.55 秒 | 9.23 秒 | 0.06 秒（131 项） |
| 2 | 0.10 秒 | 0.08 秒 | 0.03 秒（2 项） |
| 3 | 0.10 秒 | 0.07 秒 | 0（不动点） |

288 次 TMDB 请求全在第一次迭代。生产上第二次迭代起的成本主要是 qBittorrent：扫描约 538 次 `torrents/files`（runloop
调研量的不带 TMDB 的扫描 0.72 秒），检测器约 0.7 秒——估计每多一次迭代 2–3 秒；而没有要做的事时（今天的常态，0 个
可执行动作）只有一次迭代，与以前一样。

## 给以后的 `media-agent grab`（第 5 阶段）

`converge.run` 不认识 `cmd_run`：抓取模式传只有 `GRAB_DETECTORS` 的 `Registry`、`select=converge.only("grab_episode")`，
就得到同样的一轮一个执行器、不重试、反向拒绝、到顶报待做；库里别的问题照样诊断出来、一件都不执行
（`test_a_grab_only_pass_reuses_the_loop_with_only_grab_detectors`：第二次迭代那一集已在下，不再抓，不动点）。
`cmd_grab` 要自己做的、`cmd_run` 已经在做的：拿运行锁（声明 `lock=True`，否则落在 `run` 诊断与执行之间的抓取会被
诊断期的 sidecar 快照盖掉，runloop §8c）、声明 `pause=True`、`ctx.qbit is None` 时执行器整批拒绝（读不全同理）、
写健康报告（`cmd` 字段区分）。抓取之后的改名由第二次迭代里的 unrenamed-file 做——那要把 `unrenamed-file` 也放进它的
Registry、`select` 放行 `rename`，或者照旧交给下一轮 `run`，由第 5 阶段定。

## 运维要知道的

- **回到以前的单次**：`.env` 里 `MAX_ITERATIONS=1`（仍会多一次不执行的收尾诊断，列出下一轮要做的）。
- **看它迭代了几次**：`media-agent health` 的「迭代」一行、`--json` 的 `loop`（每次迭代的扫描 / 诊断 / 执行耗时与计数、
  停在哪、待做）；`run.log` 里每次迭代一行「↻ 迭代 k/N …」。今天的常态（0 个可执行动作）是一次迭代。
- **`loop.stop` 是 `moving`**：上一次迭代改了目录名 / 挪了存储，qBittorrent 60 秒内没搬完（跨卷拷贝、qBittorrent 卡住），
  这一轮不再往下迭代，`loop.unsettled` 写明是哪几个种子。偶尔一次无妨；每轮都这样就去看 qBittorrent 的搬运队列。
- **`loop_cap` 连着几轮都在**：多半是两个规则在拉锯（但不是直接的反向，否则会是 `oscillation`），或者一条规则每次迭代都
  产出新的目标。`loop.pending` 列着是哪几条。
- **`oscillation`**：两条规则在同一轮里要把一个改动来回改；被拒绝的那一个有一条 skipped 审计（「反向动作：…」、
  `reverses` 写明撤的是哪一条）。要人看哪一边的判断不对。
- **审计与回退**：一轮仍是一个批次 ID，`rollback --run` 整批撤（按审计序号倒着撤，跨迭代）；每条审计照旧一个动作一条。
- **部署后第一轮**：分集表缓存是新的键规则（按季的时效），159 个季键会全取一遍（与以前一轮的量相当），之后只取在播的
  与到期的。

## 没做的

- **`media-agent apply` 仍是单次**：它是人手里带 `--kind` / `--show` / `--limit` 的受控执行，"只做我点名的这几件"。
- **演进重扫没动**（冻结着）：`EVOLVE_MODE=propose` 时仍 `resolve_tmdb=False` 重扫一次（runloop B4）。解冻之前应改成直接用
  最后一次诊断的 state / findings。
- **不按迭代挑检测器**：全部跑，靠缓存把第二次迭代压到几秒。检测器自己变贵了（比如新的不带缓存的网络请求），
  `test_iterations_after_the_first_diagnose_without_any_network_call` 会红。
- **`RETRYABLE` 按跳过原因的开头认**（「集位被占」「目标文件已归另一个活种子」）：改这两句的措辞要同步改它；不认识的跳过
  一律按"试过了"处理——最坏是那件事等下一轮。
- **生产上的实测**：成本按 runloop 调研的生产数字与离线基座估算，没在生产上量（任务约束：不碰生产）。部署后看健康报告的
  `loop.iterations[*].scan_s / diagnose_s`。

## 测试

`tests/test_converge.py`（`lib.loop()`：`converge.run`，一个 Context、一个执行器）：
AB 重复版本一轮完成交接 → 判重 → 改名（对照：只跑一次时第一轮只交接了分类，待做 = 判重 + 改名）；「集位被占」在判重
腾出集位后重试成功；抓取元数据没等到、第二次迭代改名；改名 / 分类来回被拒、报 `oscillation`（只报一次）；删除配额跨迭代
累计（第二次迭代的隔离被「已达单轮删除数量上限 1」拦下、不再重试）；失败的动作与执行过的动作不重试；到顶报待做、收尾
诊断不写审计；正好在上限收敛不报待做；预演只跑一次；上一次迭代隔离过的路径上的赢家能当保留方；第二次迭代读不全就停、
收尾诊断读不全不列待做；只跑抓取检测器 + `only("grab_episode")`；模型选的身份钉进去的那一轮不改名、不改目录名（下一轮
才改）；目录按条目 77 改了名，第二次迭代不按新目录名重搜出同名的 88（开 / 不开模型，改之前红）；下一次迭代先等 qBittorrent 搬完目录改名的种子（改之前新目录的 `have` 被写成 [3]、旧目录留下幽灵档案），等不到就停在
`moving`、旧目录不写档案；刚 relink 的种子这一轮不按死种摘（下一轮摘）；迭代过的一轮整批回退与原来一致；第二次迭代起扫描 + 诊断 0 次网络；
TMDB 挂着一轮只等一次、拉不到的番组页一轮只请求一次（下一轮再试）。

`tests/test_run_converges.py`（`cmd_run` 原样跑）：AB 重复版本一轮完成、健康报告 `loop` 每次迭代 3 / 2 / 0、执行合计 5、
一个批次；发现历史一份、按最后一次诊断（没有判重 / 未改名 / 交接）；迭代两次以上的一轮在标题稳定闸里仍只算一轮
（`seq == 1`、新标题 pending 1 轮、没改名）；第二次迭代读不全：退出码 3、critical `refused`、原因写明第 2 次迭代、第一次
迭代的 3 项照样报、不处置、快照标成读不全；到顶 warn `loop_cap` 列出待做、输出与健康一节；两条规则打架 warn `oscillation`、
发现历史里有 `run-loop` / `oscillation`；`MAX_ITERATIONS=1` 与以前的单次一样、另报待做；预演只一次；配置默认 3、写错大声
失败；第二次迭代的扫描抛异常：crash / 1，第一次迭代的 3 项照样在报告里；抓取后元数据来晚、同一轮改名；删除配额管整轮；
目录改名后同一轮按新目录写档案（第一次迭代跳过旧目录）；qBittorrent 没搬完时停在 `moving`、退出码 0、输出与健康一节说明。
`tests/test_evolve_mode.py` 与 `test_health_report.py` 里数扫描次数的四条改成"迭代自己的扫描之外"几次（以前一轮只有一次
诊断，第二次扫描就是演进重扫）。
