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
  abrow.py        AutoBangumi 订阅行上要读的几样（番目录、库内季、番组页 id、集号偏移），只读
  plugins/        内置检测器（`adopt.py`：把 AB 订阅里只有 AB 知道的东西迁进 sidecar；`new_season.py`：订阅着的番开播新一季时登记）
  actions.py      执行器 + 隔离区 + 配额上限 + 审计日志
  converge.py     一轮之内收敛：扫描 → 诊断 → 执行重复到不动点（一个执行器、试过的不再试、撤销本轮动作的拒绝）
  grabmode.py     抓取模式（media-agent grab，每 30 分钟）：同一套 converge，只补缺的集、接手 AB 新订的番、给刚抓的收尾
  subscribe.py    media-agent subscribe：不经 AB 订阅一季（建目录 + sidecar，经执行器），预览下一次抓取会做什么
  abmode.py       AutoBangumi 的模式（AB_MODE / media-agent ab-mode）：full = AB 下载、改名；subscription = AB 只当订阅前端。切换（经执行器）、state/ab_mode.json、切换之后的核对
  audit.py        audit.jsonl 的读写：写永不抛（降级 / 转写 audit.fallback.jsonl），读两个文件一起读
  purge.py        隔离区里每一份能不能真删（按处置类别的判据）
  disposal.py     硬删除的唯一出口：预写 purge.jsonl、容量闸、run / purge 的处置
  history.py      发现历史：每轮全部发现落 state/findings/，指纹 = 规则 + 类型 + 目标（不含摘要）
  titles.py       TMDB 标题稳定闸：取不到不退回目录名、新标题连续两轮 run 才采用、30 天内不改回去（state/titles.json）
  sidecar.py      每部番的 .media-agent.json：字段归属（派生 / 身份 / 人的意图），按写的那一刻合并，坏文件不覆盖
  ledger.py       出处账本 state/ledger.sqlite：按 infohash 记每个种子是什么（番组页标题、集位、发布方编号、版本词、评分）
  ledger_backfill.py  补录账本：抓取审计 → AB 库（只读）→ 番组页 feed → ma: / manual: 标签；run 开头自动补增量
  health.py       运行健康：种子数基线（骤降且审计解释不了 → 整轮拒绝）、每轮健康报告
  notify.py       通知邮件：健康报告有变化才发（一轮最多一封），去重在 state/notify.json，永不带密钥
  runlog.py       run 的输出每行带时间与批次 ID；run.log / run.err.log 先拷贝再截断地轮转（launchd 持有描述符）
  pause.py        维护暂停：VPN 救援标记或 state/PAUSE 在时 run / apply / grab 以 75 结束（diagnose 照常）
  evolution.py    自演进：残留检测 → 提议 → 影子验证 → 提升
  cli.py          命令行入口
.agents/
  notes/          Agent Notes，路径编码 {lifecycle}/{class}/日期-标题.md
  rules/          演进出来的声明式规则（JSON），下轮自动挂载
  acks.json       已确认、先不提醒的"卡住"问题（指纹 → 理由 / 期限），版本化的用户意图
tests/
  harness/        离线测试基座：FakeQbit/FakeWeb/FakeProbe/… + LibraryBuilder
  conftest.py     自动隔离（断网、state/ 进临时目录）+ tripwire
state/            运行时数据：审计日志、隔离区、缓存、运行锁（gitignore）
deploy/           按 git tag 原地部署、launchd、VPN 救援（见 deploy/README.md）
```

## 命令

```sh
uv run media-agent scan               # 看库现状
uv run media-agent diagnose           # 跑全部规则，出问题清单（只读）
uv run media-agent apply --dry-run    # 预演修复
uv run media-agent apply              # 执行修复
uv run media-agent evolve             # 为规则盲区提议新规则（需 EVOLVE_MODE=propose）
uv run media-agent run                # 完整自治轮次：迭代到不动点（MAX_ITERATIONS，默认 3；演进默认冻结），末尾处置隔离区
uv run media-agent grab               # 抓取模式（launchd 每 30 分钟）：补缺的集、给刚抓的改名 / 判重收尾，其余治理留给 run
uv run media-agent purge --verbose    # 隔离区处置预演：每一份删不删、为什么（--apply 真删）
uv run media-agent ack <指纹> --reason …  # 确认一个卡住的问题、先不提醒（写 .agents/acks.json，要提交）
uv run media-agent health               # 最近一轮的健康报告（--run ID 指定一轮，--json 原样，--grab 看抓取的）
uv run media-agent ledger backfill      # 补录出处账本（--dry-run 只报覆盖率；run 开头自动补增量）
uv run media-agent ledger show <hash>   # 账本里某个种子是什么
uv run media-agent subscribe --tmdb ID [--season N] [--mikan ID] [--dir 名] [--require-any 词 …] [--offset N]
                                        # 不经 AutoBangumi 订阅一季：建目录 + sidecar（有审计、能回退），说出下一次抓取会做什么
uv run media-agent ab-mode [show|subscription|full] [--dry-run] [--force]
                                        # AutoBangumi 的模式：看两边各认什么 / 可逆地切 AB 的下载与改名（有审计、能回退）
uv run pytest                         # 离线测试（不联网、不碰真库）
```

**改动删改类逻辑前先写离线测试。** 用 `tests/harness` 的 `LibraryBuilder`
把事故现场搭出来（范例见 `tests/test_e2e_smoke.py`，fixture 一览见
`tests/harness/__init__.py`），`lib.cycle()` 跑一次
扫描 → 全量规则 → 执行，`lib.loop()` 像 `run` 那样迭代到不动点（`tests/test_converge.py`）。测试里触发的 failed 审计、被吞的检测器异常、
没配路由的 URL 都会让测试变红，需要时用 `@pytest.mark.allow(...)` 显式声明。
见 [离线测试基座](.agents/notes/implemented/testing/2026-09-26-offline-test-harness.md)。

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
6. **qBittorrent 的「分类」是本项目与 AutoBangumi 的所有权边界。**
   AB 的改名线程扫的是 `torrents_info(category="Bangumi", status_filter="completed")`，
   **`tag=None`——标签它从不回读**（`ab:` 只是加种子时写下的来源标记，
   不是控制开关；实测摘掉它拦不住 AB 改名）。因此：

   | 分类 | 归谁 | 谁改名 |
   |---|---|---|
   | `Bangumi` | AutoBangumi | AB（每 60 秒扫一次已完成的） |
   | `<剧名>` | 本项目 | 本项目（AB 查不到这个种子） |

   `category-consolidation` 就是交接动作：AB 下载改名完，分类一改，
   此后由本项目负责纠正。**本项目自己抓的种子直接落进剧名分类**，
   全程不进 AB 的地盘。两边因此不会对同一个文件各改各的。

   还没交接的文件（仍在 `Bangumi` 下）**不做不可逆的处置**——它此刻叫什么
   只是"AB 认为的"，不是定论。见
   [压平季误删](.agents/notes/implemented/bug-fix/2026-08-31-flattened-season-numbering.md)。

   这张表是 `AB_MODE=full`（默认）的。**订阅模式**（`AB_MODE=subscription`，第 17 条）下 AB 的改名线程停了，
   `Bangumi` 里只剩订阅那一刻 AB 补的集、名字就是发布名：它们归本项目——判重不让位，`media-agent grab` 当场交接、
   改名。判断一律问 `abmode.ab_renames(cfg)`，不要自己比较 `torrent_category == "Bangumi"` 就认定"归 AB"。
7. **修订阅时三步顺序不能反**：先改 `title_aliases`/`rss_link` → 再清"已登记但
   不在 qBittorrent"的 torrent 记录 → 最后刷新。`pull_rss` 只处理 `check_new()`
   筛出的新条目，顺序反了会让 AutoBangumi 用**仍然失效**的规则把条目重新登记一遍。
   这只在 `full` 模式下成立：订阅模式下本项目**不修 AB 的订阅、永远不叫它刷新**（第 17 条）。
8. **往媒体库里落一个名字之前，先问 `claims` 它此刻归谁。** 盘上看不到不等于没人占：
   0% 的种子、只有 `X.!qB` 的下载、只差大小写的名字（生产卷是大小写不敏感的 APFS）
   都算占用。改名、抓取后改名、relink、目录改名、回退里的每个写路径都走
   `Executor._claims()`（`ClaimIndex.check` / `check_dir` / `claims_under`）；被占就跳过
   并写明占用者，**看不全（qBittorrent 读失败）就拒绝**。新加的写路径同样要接，改完东西
   要让索引作废（执行器在写非 skipped 审计时自动作废）。见
   [路径占用](.agents/notes/implemented/architecture/2026-09-26-path-claims.md)。
9. **删除之前过删除关口**（`media_agent/gate.py`）。每一次 `trash` / `drop_torrent` 在动手前、
   按**此刻**的 qBittorrent 与磁盘、按目标本身（路径 + hash）复核，与产出它的规则无关：
   I1 不让任何集位变成零个可播文件（点名的保留方此刻真在、下完了、没被截断、同批没被删；没点名的
   ——特典也算——名字认得出集号时那一集得另有真能播的文件）；I2 不删别的种子仍声明的路径；I3 多文件种子只作废这一个
   条目（按成员定：已不下载的条目不动种子）；I4 不删封存的文件（钉子 + 复核通过 + 番组页标题满足这部番的
   `require_any`），**探测不可用当作封存**；演进规则的删除一律不执行。拒绝记 skipped「删除关口：Ix …」，看不全记
   failed。新加删除类动作必须接关口；判重类检测器要在动作里给保留方与集位（`keep_path` /
   `keep_hash` / `keep_size` / `keep_digest` / `slot`）。每条隔离记录带 `deletion`（给 purge）。见
   [删除关口](.agents/notes/implemented/architecture/2026-09-26-deletion-gate.md)。
10. **隔离区的硬删除只经 `disposal.hard_delete`**：先往 `state/purge.jsonl` 写意图并 fsync，再 unlink，
    再记完成；只删普通文件、逐个删，空目录逐层 rmdir，**永不 `rmtree`**，也不按日期整批清。删不删由
    `purge.build_pool` 按处置类别判（死种 `.!qB` 到期删；特典到期、按此刻再认仍是特典（认得出集号的，那一集
    另有正片）才删；判重要证明替代者——按 `_resolve` 认、名字有名字之外的证据（钉子 / 按季号偏移换算后的
    发布名 / 关口记下的无种子保留方）、完整、按现在的排序不输；原路径仍被种子要着的不删（名字的新主人除外）；
    合并发布 / 手动 / 其它 / 没有记录的永不自动删），
    不满 `QUARANTINE_MIN_AGE_DAYS` 的一律不删，unlink 之前按此刻再复核一遍（`purge.recheck`）。
    `MIN_FREE_GB` 只在"已证明可删"的里面提前放，证明不了的不为空间删。搬进 / 搬出隔离区先看目标卷
    放不放得下（跨卷是先拷后删）。见
    [隔离区处置](.agents/notes/implemented/architecture/2026-09-26-quarantine-disposal.md)。
11. **审计不说谎、不丢记录。** 状态只有四种：`applied`（生效了，已确认）、`skipped`（没动手）、`failed`（没生效）、
    `unknown`（也许生效了、确认不了）——"异常发生在已经发出的改动之后"不许记成 failed。新加的动作：动手前
    `_intend(逆操作)`；文件系统改动 `_effect()`（qBittorrent / AB 数据库的写调用自动记）；能按此刻状态核实的
    改动调用出错时走 `_settle`，核实生效就照常记 applied、带逆操作。写审计永不抛异常（`audit.write`，写不进去
    转写 stderr 与 `audit.fallback.jsonl`）；读审计一律用 `audit.iter_records`，不认识的状态当作"不是已生效"。见
    [审计状态契约](.agents/notes/implemented/architecture/2026-09-26-honest-audit.md)。

12. **"悄悄停摆"必须被看见。** 宽 `except` 要么说出来（`ctx.log` 带上下文、审计、往上抛、交给调用方），要么在那一行
    注释为什么不说（`tests/test_silent_excepts.py` 按语法检查）。新的健康信号接进 `health.RunHealth`（`run` 收尾不管
    成败都写报告），critical 的要让退出码非零；有状态的新检测器给集位 / 季级发现填 `Finding.subject`，指纹里**永远
    不放摘要**。要人处理、短期不会动的卡住问题用 `media-agent ack` 确认（`.agents/acks.json` 是版本化的用户意图）。见
    [运行健康](.agents/notes/implemented/architecture/2026-09-26-run-health.md)。

13. **检测只读媒体根。** `scan` 与检测器只许读媒体库；要记下来的决策放 `state/` 的缓存，要改媒体库里的东西
    （含 `.media-agent.json`）就产出动作、经执行器写（有审计、有逆操作）。`diagnose` / `apply --dry-run` /
    影子验证之后媒体根下一个字节都不变（`tests/test_diagnose_purity.py` 守着）。见
    [diagnose 改写 sidecar](.agents/notes/implemented/bug-fix/2026-09-26-diagnose-writes-sidecars.md)。

14. **"这个种子是哪一集、是什么版本"先问出处账本，不再从文件名重新猜。** 名字会被 AutoBangumi、人、别的规则改
    （2026-08-31 AB 把 `3rd Season - 08` 改成 `S01E08`，判重据此把 2016 年的第 8 集清进隔离区）。集位用
    `builtin.recorded_slot` / `ledger_view`（钉子 > 账本 > 名字，`_resolve` 已经这样），版本词用 `release_text`
    （番组页标题 + 显示名），新写的加种路径要调 `ledger.record_grab`。账本读不了时一切按名字走、健康报告说出来，
    **永不因账本拦下一轮**。见 [出处账本](.agents/notes/implemented/architecture/2026-09-27-provenance-ledger.md)。

15. **一轮 `run` 迭代到不动点，一轮一个执行器**（`media_agent/converge.py`）。扫描 → 诊断 → 执行重复到某次迭代没有新动作
    （`MAX_ITERATIONS`）：配额、批次 ID、`_grabbed` 跨迭代；按路径记的本批次状态每次迭代清掉（`Executor.new_iteration`）。
    **新加的动作**：在 `converge._TARGET` 里写明它"对谁"做（没写的按全部参数认，一轮只试一次）；有逆操作的，让
    `converge.undoes` 认得出它的反向——撤销本轮已执行动作的一律拒绝、报 `oscillation`；只有"此刻被挡着、别的动作能挪开"的
    跳过才进 `converge.RETRYABLE`；会让种子重新校验 / 搬存储的进 `converge.TOUCHING`（这一轮不按死种摘它），搬存储（setLocation）
的还要进 `converge.MOVES`（下一次扫描之前先等 qBittorrent 搬完，等不到就停在这一次迭代）。一轮只做一次的事（发现历史、标题稳定闸、卡住检测、健康报告）按**最后一次**诊断做，
    不在迭代里做。见 [一轮之内收敛](.agents/notes/implemented/architecture/2026-09-27-converge-within-a-run.md)。

16. **AutoBangumi 关掉之前，它提供的每一样先由本项目接住；人的意图只"补"不"改"。** 订阅（`subscriptions`：盘上一集都
    还没有的季也抓）、集号偏移（`episode_offsets`：按库内季）是人的意图，从 AB 迁进来、`media-agent subscribe` / 新季开播
    登记，都经专门的动作（`adopt_episode_offset` / `subscribe_season` / `create_show_dir`）：只写 sidecar 里还没有的，逆操作
    （`unset_sidecar`）只摘写下的、且没被人改过的那几项——写档案（`write_sidecar`）永远不碰它们。集号偏移只有一处口径
    （`builtin.episode_offset_for`），判重、改名、`have`、出处账本、抓取都问它。每 30 分钟的 `media-agent grab`
    （`grabmode`）只做抓取与收尾：新规则要进抓取模式，得在 `grabmode.registry()` 里注册、在 `Scope.select` 里写明挑法，
    删除照样过关口；其余治理留给 6 小时的 `run`。见 [接手 AB 订阅](.agents/notes/implemented/architecture/2026-09-27-ab-adoption.md)、
    [订阅](.agents/notes/implemented/architecture/2026-09-27-subscriptions.md)、
    [抓取模式](.agents/notes/implemented/architecture/2026-09-27-grab-mode.md)。

17. **AutoBangumi 的模式只经 `media-agent ab-mode` 切；按模式开关，不删为 AB 写的代码。** `full`（默认）= AB 拉 RSS、下载、
    改名；`subscription` = AB 只当订阅的前端（它的 RSS 与改名两个线程关掉，WebUI 订阅照旧），抓取与改名全归本项目。
    - **判断只有一处**：`abmode.ab_renames(cfg)`（`Bangumi` 分类归不归 AB）与 `abmode.ab_downloads(cfg)`（只为 AB 存在的规则
      开不开）。新写的"因为 AB 在下载 / 改名"才需要的逻辑都按它们开关——`full` 是回退的路，切回去当场恢复。
    - **订阅模式下永远不叫 AB 刷新**（`refresh_all` / `refresh` 会让它当场下载，两个开关管不住），不写它的订阅，抓的种子
      不打 `ab:` 标签。
    - **改 AB 的配置只能整份写回**（漏掉的段 AB 会退回默认值）；开关只在程序重启时生效；读回核对之后才改本项目的模式
      （`state/ab_mode.json`，盖过 `AB_MODE`；`.env` 代码不写）。说不清就记 unknown、模式不动。
    - 切换之后 AB 真停了没有，`/api/v1/status` 说不出来：按切换时的基线核对（`abmode.activity`，健康报告的
      `ab_still_polling` / `ab_added_outside_subscribe` / `ab_mode_unverified`）。
    - AB 里停用订阅（`deleted=1`）会让本项目看不见这条订阅（`AutoBangumiDB.bangumi` 只读 `deleted=0`）：不要为了"停"
      而停用订阅。
    见 [AB 的模式](.agents/notes/implemented/architecture/2026-09-27-ab-mode.md)，步骤与回退见 `deploy/README.md`。

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

**默认冻结**（`EVOLVE_MODE=off`，2026-09-26 起）：`run` 不跑演进、不调 LLM、
不往 `.agents/` 写任何东西。2026-08-20 之后连续 147 轮提议 0 条、现有演进规则
全无动作，冻结行为中立；而生产改成按 git tag 部署后，工作区里未入库的规则/笔记
会被部署的漂移闸门拦下。要演进就设 `EVOLVE_MODE=propose`，产出提交入库、打 tag
再部署。演进规则带的动作无论哪种模式都不自动执行。

## 写 Agent Note 的时机

任何非平凡改动都要在同一次提交里新增或更新一条 Agent Note：行为变化、架构决策、
跨文件契约、流程工具、磁盘/配置格式。类别取自闭集：
`feature` / `bug-fix` / `simplification` / `architecture` / `process` / `testing`。

**提交前自查**：`git diff --cached --stat` 里有 `media_agent/`、`deploy/`、`.github/`、
`pyproject.toml` / `uv.lock`、`.gitignore` 或配置格式的改动，就必须同时看到 `.agents/notes/`
的改动（只补测试、只改文案的提交除外）。事后在最后一个提交里补一篇总笔记不算数——
单独检出、bisect、cherry-pick 中间那个提交时，它没有决策记录（2026-09-26 审查：
653aff2..c2eca6f 五个提交就是这样，见 `process/2026-09-26-tag-deploy-and-run-lock.md` 的追认）。
笔记里引用调研编号时，编号要能在
[审计编号索引](.agents/notes/implemented/process/2026-09-26-phase1-audit-index.md) 里查到。
