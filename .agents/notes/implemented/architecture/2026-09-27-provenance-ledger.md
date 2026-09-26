# 出处账本：按 infohash 记下每个种子是什么

**日期**: 2026-09-27
**状态**: implemented / architecture
**触发**: 整改第 4 阶段。一个种子**是哪一集、是哪个版本**，只在两个时刻知道得最清楚——抓取那一刻、
AutoBangumi 登记它那一刻——而之后每条规则都在从文件名、种子显示名重新猜：

- 2026-08-31 Re:Zero：AB 把 `[Fyy Raws] … 3rd Season - 08` 改名成 `S01E08`，判重按文件名把 2016 年真正的
  第 8 集（1.31 GB）清进了隔离区。发布方明写的"第三季"，到判重那一刻已经没人记得（第 2 阶段之后仍在：隔离可回退、
  purge 不硬删，但隔离本身还会发生）。
- LoliHouse 的内部名只写 `ASSx2`，「简繁内封字幕」只在番组页标题里；尼古喵喵的「邪竜解放版」同样只在标题里。
  探测不可用时，复核（`meets_requirements`）与择优（`_prefer_score` / `_rank_for_keep`）只能按内部名判错。
- 钉着 `ma:S01E58`、还叫着发布名 `- 08` 的已下完文件，`have` 按名字算成第 8 集，第 58 集被再抓一遍（critic N14；
  生产 28 个 infohash 被抓了不止一次，入间 S4E20 一集 7 次）。
- 抓取时的发布日期、结构化的偏好评分、落选了几个只进了 Finding 的 evidence；审计只存 `args` 与摘要（state 调研 H1）。

## 存储（`media_agent/ledger.py`）

`state/ledger.sqlite`：WAL（`run` 与人手的 `ledger backfill` 可以同时开着）、`busy_timeout` 5 秒、结构版本在
`PRAGMA user_version`。一行 = 一个 infohash：

| 列 | 是什么 |
|---|---|
| `source` | 谁加的：`media-agent` / `autobangumi` / `manual` / `unknown`（知道是什么、不知道谁加的） |
| `status` | `active`；回退了抓取的是 `retracted`——行留着，只是不再替它的集位作保 |
| `mikan_title` / `mikan_url` / `pub_date` | 番组页上的发布标题、种子链接（URL 文件名就是 v1 infohash）、发布日期 |
| `show_dir` / `season` / `episode` | 落在哪部番；**库内集位**（`ma:` 钉子编码的那个）。抓取写的是定论；补录的是补录那一刻按标题算的 |
| `declared_season` / `raw_episode` | 发布方自己写的季号与原始集号（`naming.declared_seasons` 按 ` / ` 分段各认一次） |
| `versions` | 标题里命中的版本词（偏好规则的 prefer / avoid 关键词 + BDRip） |
| `verdict` / `chosen_reason` | 偏好评分（acceptable / score / passed / penalties）与为什么选它 |
| `grabbed_at` / `ab_bangumi_id` / `run_id` | 何时抓（补录的是 AB 登记的，没有时间就空）、AB 番组 id、哪一轮写的 |
| `notes` / `evidence` | 备注（409、撤销……）；抓取时 Finding 里的证据（落选的、按日期 / 别季排除的） |

另有 `lookup_miss`：补录时番组页里找不到的种子，`MISS_TTL`（7 天）内不再为它们拉番组页。

## 契约

- **一个 infohash 一行，抓取写的是定论。** `record_grab` 覆盖之前补录的；409（种子早就在）时谁加的不改、记一笔。
  `upsert_backfill` 只填没有的行，永不覆盖——幂等，重复补录什么都不改。
- **账本记事实，不记结论。** 补录行的集位只是补录那一刻按当时的 `season_offsets` 算的；规则要用时按**此刻的**
  换算关系从标题重算（人后来补上的 `season_offsets` 立刻生效）。抓取行的集位是抓取器定的，不重算。
- **坏了不拦路、不重建。** 打不开、坏了、结构版本比代码新 → `LedgerUnavailable`，文件原样不动（坏账本要人看，
  重建会丢掉抓取时才有的证据）。读的一方用 `load_rows`：永不抛、给一句原因；还没有账本时不凭空建库
  （`diagnose`、purge 的重扫不该在 `state/` 里留下空库）。
- **放在 `state/` 而不是 sidecar**（state 调研 H5）：sidecar 按番、会被整份重写；账本按种子、只增不改。

## 写的人

- **抓取**（`_op_grab_episode` → `Executor._ledger_grab`）：加种成功、409、或出错后按 infohash 核实种子在，
  当场记一行（infohash 从 .torrent 算）——番组页标题与链接、抓取器定的集位、Finding 里以前丢掉的证据
  （发布日期 `chosen_pub`、结构化评分 `verdict_detail`、候选数、落选了几个、按日期 / 别季排除的、番组页 id、
  播出日期）。409 而账本里没有它：来源记 `unknown`（不冒认是自己加的），补录认出是谁加的再补上。
  预演不记。账本写不进去**不让抓取变成失败**（种子已经加进去了）：审计记 `ledger_error`、日志一行。
  审计记录与逆操作都带上 `infohash`。
- **回退抓取**（`ungrab_episode`）：sidecar 的 `have` 摘掉这一集，账本那一行标 `retracted`（行留着）；
  账本写不进去写进回退汇报，不让回退失败。

## 补录（`media_agent/ledger_backfill.py`）

`media-agent ledger backfill [--dry-run]` 人手跑全量；每轮 `run` 扫描之后、诊断之前自动跑一次**增量**（只看还没有
一行、或来源还是 `unknown` 的种子，用扫描读到的那份种子列表，不再问一遍 qBittorrent）。来源按可信程度：

0. **抓取审计**（`grab_episode`；applied / unknown / **failed** 都认——补录只针对此刻就在 qBittorrent 里的种子，
   它在，加种就发生过：2026-09-16 … 09-26 的 12 次抓取种子加上了、之后 NameError，全记成了 failed）。集位是
   抓取器的，照记为定论。URL 的文件名就是 infohash。这个 infohash 的抓取**全是 409**（`already_present`）的，
   种子早就在、不是本项目加的：来源记 `unknown`，留给下一步 AB 库补上是谁加的。
1. **AutoBangumi 库的 `torrent` 表**，只读（`mode=ro`，不停容器）：`name` = 番组页标题、`url` 里是 infohash
   （`qb_hash` 永远 NULL），连带番组行的季与 `episode_offset`。
2. 还剩下的、所在的番 sidecar 里有 `mikan_id`（或订阅链接里带番组 id）的：拉番组页 feed（与抓取共用 1 小时缓存），
   按 enclosure URL 的文件名认——不下 .torrent。认不出的记 `lookup_miss`，7 天内不再为它们拉番组页。
   认出了、但不知道谁加的：来源 `unknown`（钉着 `ma:` 的是本项目）。只为还完全不知道是什么的种子拉——已有一行、
   只差"谁加的"的，番组页答不了，不为它每轮拉一遍。
3. 最后只剩标签的：`ma:` 钉子（只有本项目的抓取打它，集位就是钉子）、`manual:`（人手加种时自己打的）；标题不知道，不编。

集位按读账本的同一套算（`naming.title_slot`，见下面"集位"）；钉着 `ma:` 的按钉子；换算不了的（含声明了不止一个
季号的）留空。只补没有的：幂等。（最初按文件名那一套 `naming.release_slot` 算，2026-09-27 改掉，原因见下。）
任何一个来源读不了只说一句（`problems`），其余照补；账本打不开整个跳过，不重建。`run` 里补录出错说一句、照常跑。

离线核对（生产 2026-09-26 的只读快照，不联网、不含第 2 步）：539 个种子补上 514 个——AB 460（其中 2 个审计里只有
409 的抓取、集位按抓取器的）、本项目 52（审计里的抓取 + 只有钉子的）、人手 2；剩 25 个（多为 `group:` 标签的
人手加种与合集）。有集位的 498 个；声明了别的季、换算不了、集位留空的 14 个（正相反的你与我 12 个
`第二季 - 13…23`，Re:Zero 2 个）。

## 读的人

扫描（`scan.build_state`）读一次账本（`load_rows`），把行挂到每个有种子的文件上（`MediaFile.ledger`），并记下
种子要下载的视频条目数（`MediaFile.torrent_videos`）。`run` 开头补录之后再挂一遍（`scan.attach_ledger`）。
读不了 → `state.ledger_problem`，这一轮按没有账本走。规则不自己开账本，问文件上挂着的那一行。

### 集位（`builtin.ledger_view` / `_resolve`）

账本说得上话的前提：有种子、行有效（撤销的不算）、种子只有一个要下载的视频（番组页标题说的是整个发布，
合集、合并发布说不了单个文件）、标题里**发布方声明了季号**（没声明的，标题与文件名是同一套编号）。

- 抓取行：集位是抓取器定的（与 `ma:` 钉子同源），照它；
- 声明了**不止一个**季号（CR 系的 `第三季 / … S01E25`：中文段是季、英文段是 TMDB 的连续编号）：说不清按哪一季编号，
  没话说（`ledger.release_facts` 也记 None，不取最小的）；
- 其余：放在文件**此刻所在的库内季**（季目录 > 文件名的 Sxx > AB 的季 > 1），按此刻的 `season_offsets` 与抓取挑候选
  **同一处**（`naming.slot_in_season`）换算：声明的就是这一季 → 原始集号；声明了别季、有偏移 → 换算；特典位只认标着
  特别篇 / OVA / SP 的、不加偏移。AB 的 `episode_offset` 只对它订阅的那一季、对标题里的原始集号换算（标题不是 AB
  改出来的名字，`S01E25` 也照换——AB 自己就是这么做的）。算得出是 `slot`，算不出（声明的季号与库内不同、没有换算；
  正片躺在特典位）是 `conflict`。**账本永远不把文件挪到别的季**：`_resolve` 只在账本的季 = 文件所在的季时才改口。
- **2026-09-27 审查修正**：最初按文件名那一套（`naming.release_slot`）算——标题里的 `Sxx` 定季、声明的季号取最小的、
  特典位也加正片季的偏移。按 09-27 生产快照补录之后，全库唯一一处"账本压过文件名"是错的：《超超超超超喜欢你的100个
  女朋友》AB 订阅 37（第三季、-24）改好的 `Season 3/… S03E01…S03E12`，标题 `第三季 / … S01E25` 算成 (1, 25)，
  unrenamed-file 提议把 10 个文件改成 `S01E25`…`S01E36`（分类已交接，AB 不会改回来），sidecar-sync 把 S3 进度并进 S1。
  同一套算法下 Re:Zero 的 `第四季 / … S04E15` 账本算 (4, 15)、抓取算第 81 集；`第三季 OVA - 01` 在 Season 0 被算成
  S00E25（药屋少女、Re:Zero 生产上都有 Season 0 + `season_offsets`）。

`_resolve`：钉子 > 账本的 `slot`（与文件名不同时）> 文件名 > 种子名。`conflict` 时 `_resolve` 仍按文件名——
单独占着一个集位的不变成"认不出"；**撞进同一个集位时**判重把它剔出这一集的取舍（不当保留方、也不当输家），
报 `season_numbering_conflict`（important，登记换算关系就好）。取舍的理由：生产上正相反的你与我 12 个 ANi
`第二季 - 13…23` 已按 TMDB 连续编号改成 S01E13…23，是对的；同形的 Re:Zero `3rd Season - 08` → S01E08 是错的——
没有换算关系就分不出来，只有撞车时才要紧（离线核对：换算不了的 14 个里只有撞车的会被报）。
`unrenamed-file` 对已经规范的名字同样认账本的 `slot`（与钉子一样，集号不对就改回来）。

### 已有 / 在下（`builtin.recorded_slot`，critic N14）

"名字之外记下来的集位"：`ma:` 钉子 > 账本（抓取器定的集位不要求声明季号；其余要 `ledger_view` 算得出 `slot`）。
`kernel.have_episodes`（sidecar-sync 的 `have`、缺集检查、抓取的 `_disk_episodes`）与 `grab._inflight` 都先问它、
再看名字。以前两边各看各的：`have` 按名字把钉着 S01E08 的发布名 `- 03` 算成第 3 集，第 8 集被再抓一遍（409、
再改一次显示名与文件名）；`_inflight` 让规范名压过钉子。认不出的照旧按名字（包括 `conflict`：单独一个的按连续编号
改好的名字是对的）。

### 隔离区处置（`purge`）

- 替代者是不是这一集（`_identity_problem`）：钉子之后先问账本——`recorded_slot` 等于这个集位 → 作保（抓取器定的集位
  与钉子同源，内部名认不出集号的也作保）；不等 → 不作保；`ledger_view` 是 `conflict` → 不作保（显示名不写季号、
  "认得出"这一集的也不行：`[Fyy Raws] Re Zero - 08` 的番组页标题是「第三季 - 08」）。账本没话说时照旧看显示名。
- 隔离记录的集位（`_slot_of`）：`deletion.slot` > 摘要 > **被隔离的那个种子在账本里的集位**（有效行、那个种子只有
  一个要下载的文件）> 明写季号的原文件名。
- 复排（`_rank_problem`）：隔离的那份带上它的番组页标题（`release_text`）。
- 账本一次 `build_pool` 读一次（`_Pool.ledger`），读不了当没有。删除关口 I1 / I2 没动：它们按此刻的 qBittorrent 与
  磁盘复核，不按谁的说法。

### 版本（`builtin.release_text`）

名字证据 = 番组页标题（账本有的话）+ 种子显示名（没有种子是文件名）。复核（`meets_requirements`：探到中文轨先认轨，
探不到才轮到名字）、择优（`_prefer_score` 的发布一档；文件一档仍只看文件名，合并发布的两份靠它分）、判重排序
（`_rank_for_keep` 的画质 / 简繁 / BDRip 标记）都用它。标题说的是整个发布——撤销了的行也算（撤销的是对集位的
担保，不是"它是什么"）。探测可用时轨道优先，标题只在探不到时说话，所以不会因为标题写了「简繁」就压过真实的零字幕轨
（`meets_requirements` 的"名字说有、容器里没有 = 内嵌硬字幕"那条照旧）。

### 这部番只保留某个版本（`builtin.requirement_problem` / `_meets_show`）

sidecar 的 `require_any` 以前只在抓取挑候选时用。封存时也核对：钉着的文件，账本里它的番组页标题（+ 显示名 +
文件名）一个要求的词都没有 → 不封存（`seal_failed`，版本不对与探不探得到轨无关，也不当"封存不可知"护着）；
账本里没有标题的照旧封存（内部名里常常没有版本词，凭它判"不满足"会误伤）。判重排序（没封存时）第一档是满不满足
`require_any`（这一档按名字证据算，没有账本的也算——排序不删东西，只决定谁更该留），再比画质。删除关口 I4 用
同一个判据（`gate._show_requirement` 读账本与 sidecar，读不了当满足）：只改判重不改关口，判重换了方向关口也删
不掉那份 TV 版，这一集每轮卡着。

## 健康报告（`RunHealth.ledger`）

`run` 补录之后记一节 `ledger`：这一轮的种子里有出处的 / 没有的 / 其中加进来超过 `PROVENANCE_GRACE_H`（24 小时）的
（全部短 hash 与按加入时间倒序的样本）、本轮补录结果、账本读不了的原因、上一轮的"超时没出处"数。两条 warn：

- `ledger_unavailable`：账本读不了。不拦路（退出码照旧）：规则按没有账本走，但要人看。
- `provenance_unknown_grew`：超时没出处的比上一轮**多了**，写明新冒出来的是哪几个。只看增长——开张时就查不到出处的
  （离线核对 25 个，多为 `group:` 标签的人手加种与合集）每轮都报就成了噪音；24 小时内的不算（AB 登记、抓取记账都在
  加种的同一刻，补录每轮开头跑，正常的种子一轮之内就有出处）。没读到种子列表的一轮不记（不拿"0 个"当下一轮的基准）。

## 运维要知道的

- 部署后第一轮 `run` 开头会补录全部种子（离线核对：审计 + AB 库就能补上 514/539，不联网）；剩下的、所在的番有
  `mikan_id` 的会各拉一次番组页（与抓取共用 1 小时缓存），认不出的 7 天内不再拉。部署前后可以先
  `media-agent ledger backfill --dry-run` 看覆盖率与没有出处的种子。预演的 `run`（`--dry-run`、`AUTO_APPLY=false`）
  同样补录——账本记的是事实（像缓存一样在 `state/`），不是对媒体库的改动。
- 看一个种子：`media-agent ledger show <infohash 前 6 位以上>`。
- 账本认错了一集：**钉子压过账本**——在 qBittorrent 里给那个种子打上正确的 `ma:SxxEyy`；补录行的集位按此刻的
  `season_offsets` 重算，改 sidecar 就生效。报了 `season_numbering_conflict` 的，登记 `season_offsets` 即可。
- 账本坏了（健康报告 `ledger_unavailable`）：规则按名字走、照常跑；把 `state/ledger.sqlite` 挪开（连同 `-wal` / `-shm`），
  下一轮会从审计与 AB 库补回来——抓取那一刻才有的发布日期与评分补不回来，所以先留一份坏文件再挪。

## 没做的

- **删除关口没改**（除 I4 的第三条判据）：I1 的"另有可播文件"（`gate.other_holders`）与保留方复核（`_keeper_problem`）
  仍按钉子与名字认集位——它们按此刻的 qBittorrent 与磁盘复核，账本只会让它们更松或更严，第 2 阶段的不变量不动。
  检测器一侧已经不再产出 2026-08-31 那样的删除。
- 生命周期事件（state 调研 H3：改名、relink、目录搬迁、摘种、隔离）没记进账本；账本只回答"它是什么"，不回答
  "它后来怎样了"——审计已经记着后者。
- 补录不看回退：审计里被回退过的抓取（`ungrab_episode`）补录时仍记为有效行（回退只撤销 `have`，种子与钉子都还在）。
- `manual` 只来自 `manual:` 标签；`group:` 标签的人手加种（离线核对里的大部分剩余）仍是"没有出处"。
- 演进规则的 DSL 还没有账本字段（`_FIELD_GETTERS`）。
- AB `episode_offset` 番的抓取（critic N13）不在这一步。

## 测试

`tests/test_ledger_health.py`：报告带覆盖率、输出有「出处」一行、第一轮不报；只在增长时报、写明新的是哪个、不再增长
不报；24 小时内的不算；账本读不了报 warn、退出码 0、文件不动（改之前全红）。

`tests/test_ledger_purge.py`：显示名认得出 S01E08、番组页标题是第三季的替代者不作保（改之前原片到期被硬删）；没有账本
时照旧作保（对照）；内部名认不出集号、抓取器定过这一集的替代者作保；隔离记录认不出集位时按账本认（改之前 3 条红）。

`tests/test_ledger_have.py`：钉着 S01E08 的发布名 `- 03` 算第 8 集（两种口径都算）、不再抓第 8 集；钉子没了、账本的
抓取行照样认；两样都没有时按名字（对照）；在下的文件钉子压过规范名（改之前 3 条红）。

`tests/test_ledger_require_any.py`：`require_any` 之前抓的钉着的 TV 版不封存、被当输家隔离、关口放行（改之前红）；
没有账本时照旧封存；关口对满足要求的封存照旧 I4 拒绝。

`tests/test_ledger_versions.py`：标题拼在内部名前；探测不可用的 LoliHouse 凭番组页标题通过复核；「邪竜解放版」只在标题里
也加分；判重排序里简体压过繁体；端到端：钉着的 LoliHouse 探测不可用也封存这一集、另一份照常判输（改之前 5 条全红）。

`tests/test_ledger_identity.py`：2026-08-31 端到端（整条 `run`：补录认出「第三季 - 08」，原片不进隔离区，报
`season_numbering_conflict`；改之前原片被隔离）；配了 `{"3": 50}` 改名改回 S01E58、不判重；没有账本时行为不变；
单独一个的换算不了不报；抓取行照抓取器的集位；撤销的 / 多视频的 / 没声明季号的不参与；坏账本按文件名、说出来；
《100个女朋友》第三季（`第三季 / … S01E25`、AB -24）按文件名、补录不定集位；Re:Zero `第四季 / … S04E15` 与抓取同算
第 81 集；Season 0 的 `第三季 OVA - 01` 不加偏移、正片躺在 Season 0 是 conflict；AB 的 -24 不减到别季（改之前 5 条全红）。

`tests/test_ledger_backfill.py`：AB 库补录（只读、不停容器、集位留空不猜）；`season_offsets` 与 `episode_offset`
的换算；审计里的旧抓取（含记成 failed 的）；番组页按 URL 认出、钉子的按钉子；认不出的报出来、7 天内不再拉番组页；
幂等；预演不建库；抓取行不被覆盖；坏账本不重建；命令行报覆盖率；`run` 开头自动补；标签兜底。

`tests/test_ledger_grab.py`：抓取记下它是什么（集位是抓取器的 58 不是标题里的 08、发布方编号、评分、落选数、
番组页 id）；409 也记（来源 unknown）；预演不记；账本坏了抓取照样 applied、账本文件不动；回退只标撤销；
补录在前、抓取在后时集位按抓取器的、来源仍是 AB。

`tests/test_ledger.py`：新库是 WAL、有忙等、有结构版本；抓取行回答"它是什么"（集位、发布方编号、版本词）；
补录只填空、不覆盖、幂等；409 时来源不改；撤销留行、不再作保；坏文件与新版本的库报原因且一个字节不动；
没有账本时只读的一方不建库；两条连接共用；从 URL 认 infohash。
