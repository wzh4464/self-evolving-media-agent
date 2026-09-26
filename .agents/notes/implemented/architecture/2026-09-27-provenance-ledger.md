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
   抓取器的，照记为定论。URL 的文件名就是 infohash。
1. **AutoBangumi 库的 `torrent` 表**，只读（`mode=ro`，不停容器）：`name` = 番组页标题、`url` 里是 infohash
   （`qb_hash` 永远 NULL），连带番组行的季与 `episode_offset`。
2. 还剩下的、所在的番 sidecar 里有 `mikan_id`（或订阅链接里带番组 id）的：拉番组页 feed（与抓取共用 1 小时缓存），
   按 enclosure URL 的文件名认——不下 .torrent。认不出的记 `lookup_miss`，7 天内不再为它们拉番组页。
   认出了、但不知道谁加的：来源 `unknown`（钉着 `ma:` 的是本项目）。
3. 最后只剩标签的：`ma:` 钉子（只有本项目的抓取打它，集位就是钉子）、`manual:`（人手加种时自己打的）；标题不知道，不编。

集位按 `naming.release_slot` 算（判重分桶也用它：名字里的 Sxx > 季目录 > AB 的季；声明的季号只有 `season_offsets`
有它才换算；AB 的 `episode_offset` 只换算原始集号）；钉着 `ma:` 的按钉子；换算不了的留空。只补没有的：幂等。
任何一个来源读不了只说一句（`problems`），其余照补；账本打不开整个跳过，不重建。`run` 里补录出错说一句、照常跑。

离线核对（生产 2026-09-26 的只读快照，不联网、不含第 2 步）：539 个种子补上 514 个——AB 458、本项目 54（审计里的
抓取 + 只有钉子的）、人手 2；剩 25 个（多为 `group:` 标签的人手加种与合集）。有集位的 498 个；声明了别的季、
换算不了、集位留空的 14 个（正相反的你与我 12 个 `第二季 - 13…23`，Re:Zero 2 个）。

## 读的人

扫描（`scan.build_state`）读一次账本（`load_rows`），把行挂到每个有种子的文件上（`MediaFile.ledger`），并记下
种子要下载的视频条目数（`MediaFile.torrent_videos`）。`run` 开头补录之后再挂一遍（`scan.attach_ledger`）。
读不了 → `state.ledger_problem`，这一轮按没有账本走。规则不自己开账本，问文件上挂着的那一行。

### 集位（`builtin.ledger_view` / `_resolve`）

账本说得上话的前提：有种子、行有效（撤销的不算）、种子只有一个要下载的视频（番组页标题说的是整个发布，
合集、合并发布说不了单个文件）、标题里**发布方声明了季号**（没声明的，标题与文件名是同一套编号）。

- 抓取行：集位是抓取器定的（与 `ma:` 钉子同源），照它；
- 其余：按此刻的 `season_offsets` / AB `episode_offset` 从标题重算（`naming.release_slot`）。算得出是 `slot`，
  算不出（声明的季号与库内不同、没有换算）是 `conflict`。

`_resolve`：钉子 > 账本的 `slot`（与文件名不同时）> 文件名 > 种子名。`conflict` 时 `_resolve` 仍按文件名——
单独占着一个集位的不变成"认不出"；**撞进同一个集位时**判重把它剔出这一集的取舍（不当保留方、也不当输家），
报 `season_numbering_conflict`（important，登记换算关系就好）。取舍的理由：生产上正相反的你与我 12 个 ANi
`第二季 - 13…23` 已按 TMDB 连续编号改成 S01E13…23，是对的；同形的 Re:Zero `3rd Season - 08` → S01E08 是错的——
没有换算关系就分不出来，只有撞车时才要紧（离线核对：换算不了的 14 个里只有撞车的会被报）。
`unrenamed-file` 对已经规范的名字同样认账本的 `slot`（与钉子一样，集号不对就改回来）。

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

## 测试

`tests/test_ledger_require_any.py`：`require_any` 之前抓的钉着的 TV 版不封存、被当输家隔离、关口放行（改之前红）；
没有账本时照旧封存；关口对满足要求的封存照旧 I4 拒绝。

`tests/test_ledger_versions.py`：标题拼在内部名前；探测不可用的 LoliHouse 凭番组页标题通过复核；「邪竜解放版」只在标题里
也加分；判重排序里简体压过繁体；端到端：钉着的 LoliHouse 探测不可用也封存这一集、另一份照常判输（改之前 5 条全红）。

`tests/test_ledger_identity.py`：2026-08-31 端到端（整条 `run`：补录认出「第三季 - 08」，原片不进隔离区，报
`season_numbering_conflict`；改之前原片被隔离）；配了 `{"3": 50}` 改名改回 S01E58、不判重；没有账本时行为不变；
单独一个的换算不了不报；抓取行照抓取器的集位；撤销的 / 多视频的 / 没声明季号的不参与；坏账本按文件名、说出来。

`tests/test_ledger_backfill.py`：AB 库补录（只读、不停容器、集位留空不猜）；`season_offsets` 与 `episode_offset`
的换算；审计里的旧抓取（含记成 failed 的）；番组页按 URL 认出、钉子的按钉子；认不出的报出来、7 天内不再拉番组页；
幂等；预演不建库；抓取行不被覆盖；坏账本不重建；命令行报覆盖率；`run` 开头自动补；标签兜底。

`tests/test_ledger_grab.py`：抓取记下它是什么（集位是抓取器的 58 不是标题里的 08、发布方编号、评分、落选数、
番组页 id）；409 也记（来源 unknown）；预演不记；账本坏了抓取照样 applied、账本文件不动；回退只标撤销；
补录在前、抓取在后时集位按抓取器的、来源仍是 AB。

`tests/test_ledger.py`：新库是 WAL、有忙等、有结构版本；抓取行回答"它是什么"（集位、发布方编号、版本词）；
补录只填空、不覆盖、幂等；409 时来源不改；撤销留行、不再作保；坏文件与新版本的库报原因且一个字节不动；
没有账本时只读的一方不建库；两条连接共用；从 URL 认 infohash。
