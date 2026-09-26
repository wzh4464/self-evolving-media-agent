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

## 测试

`tests/test_ledger.py`：新库是 WAL、有忙等、有结构版本；抓取行回答"它是什么"（集位、发布方编号、版本词）；
补录只填空、不覆盖、幂等；409 时来源不改；撤销留行、不再作保；坏文件与新版本的库报原因且一个字节不动；
没有账本时只读的一方不建库；两条连接共用；从 URL 认 infohash。
