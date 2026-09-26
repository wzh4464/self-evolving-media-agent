# sidecar 的字段归属：派生的照算，人写的不碰，坏了不覆盖

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 整改第 4 阶段（身份与纯度）前的状态测绘。`.media-agent.json` 有五个写的人（检测期的抓取、
`_op_grab_episode`、`_op_write_sidecar`、回退的 `restore_sidecar` / `ungrab_episode`），没有一处知道哪些字段
是自己的：

- `_op_write_sidecar` 拿**诊断期**算的快照**整份覆盖**。诊断之后人改的 `season_offsets` / `require_any` /
  `notes`、另一个进程写的、回退写回去的，都被盖掉；本轮抓取（op 0）写进去的新集要靠 `_grabbed` 专门并回来。
- `load()` 丢掉本版本不认识的键：以后加一个字段（比如出处账本），旧代码写一次就没了。
- `load()` 遇到坏 JSON 静默返回默认值，下一次写就拿默认值盖掉那份文件——人写的换算关系、版本要求一起没了。
  抓取的记账（读-改-写）正是这样：先读成默认值，再整份写回。
- 临时文件名固定叫 `.media-agent.json.tmp`，两个写的人会抢同一个。

## 归属表（`media_agent/sidecar.py`：`DERIVED` / `IDENTITY` / `USER_INTENT` / `BOOKKEEPING`）

| 字段 | 归属 | 谁写 | 写档案时（`merge_for_write`） |
|---|---|---|---|
| `canonical_title` | 派生（= 目录名） | sidecar-sync | 用这一轮算的 |
| `tmdb_title` | 派生（稳定后的 TMDB 标题） | sidecar-sync | 用这一轮算的；`pinned` 里有它就不动；payload 是另一个 tmdb_id 的就不要 |
| `aliases` | 派生、只增不减 | sidecar-sync（AB 规则）、抓取（发布名片段） | 并集 |
| `bangumi_id` | 派生（AB save_path） | sidecar-sync | 用这一轮算的 |
| `sources` | 派生（AB 订阅 + RSS 缓存） | sidecar-sync | 用这一轮算的 |
| `seasons` | 派生（`have` 按盘上规范名；`aired/total/next_air/seasonal` 按 TMDB 缓存） | sidecar-sync、抓取（`have` 加一集）、回退（`have` 减一集） | 用这一轮算的，再并回**本轮抓的**集（`_grabbed`） |
| `tmdb_id` / `tmdb_source` | 身份（扫描照它认，见 `architecture/2026-09-26-tmdb-identity-pinning.md`） | 只在**还没有**时填：sidecar-sync 按扫描搜到的（`search`）、`pin_tmdb` 动作（模型选的，`llm`）；之后只有人改（可写 `human`） | 文件里已有就不动；没有才填 |
| `season_offsets` / `require_any` / `notes` | 人的意图 | 只有人 | 一律以此刻文件里的为准，payload 里的旧值不算数 |
| `mikan_id` | 人的意图（2026-09-26 起代码不再写，见 `bug-fix/2026-09-26-diagnose-writes-sidecars.md`） | 人（现存值有历史上自动写的） | 同上 |
| `pinned` | 人的意图：列出的字段由人说了算 | 只有人 | 同上 |
| `schema_version` / `updated_at` | 记账 | 每次写 | 每次写时重设 |
| 本版本不认识的键 | —— | —— | 原样保留 |

测试要求 `Sidecar` 的每个字段恰好归一类——加字段时必须先想清楚它归谁。

## 写的路径

- **`_op_write_sidecar`**：写的这一刻重新读文件，`sidecar.write_merged(show_dir, payload, adjust)`；`adjust`
  并回本轮抓的集（第 0 阶段的 `_grabbed` 合并照旧，只是改成作用在合并结果上）。诊断期的目录本轮已经改名 / 搬走
  （`rename_show_dir` op 8 在它之前）：记 skipped「目录已不在」，下一轮按新目录重算——以前往旧路径写要么失败、要么
  凭空建目录。
- **`_op_grab_episode` / 回退 `ungrab_episode`**：`sidecar.update(show_dir, mutate)`——按此刻的文件读-改-写，
  不认识的键保留。
- **`sidecar.save`**（测试构造器等）：同样保留不认识的键、同样拒绝覆盖坏文件。
- 物理写只有一处 `write_text_atomic`：同目录 `mkstemp` 临时文件 + `os.replace`，临时名各不相同，失败时删掉临时文件。

## 坏掉的 sidecar

- 解析不了（不是 JSON、顶层不是对象、读不了）= 坏。`load()` 给读的一方照旧返回默认值（检测继续跑），
  `load_checked()` 同时返回问题。以前顶层是数组时 `data.items()` 直接抛。
- **写的一方一律拒绝**：`save` / `update` / `write_merged` 先读此刻的文件，坏了就拷一份
  `.media-agent.json.corrupt-<时间>`（已有内容相同的备份就不再拷），抛 `SidecarCorrupt`。
  `_op_write_sidecar` 记 skipped（原因 + 备份位置）；抓取照常加种、记账那一步记 `sidecar_error`；回退的
  `restore_sidecar` / `ungrab_episode` 这一步跳过并写明原因。备份不算库的改动、没有逆操作：它只是把原样内容多留一份。
- sidecar-sync 报 `sidecar_corrupt`（important，带问题与现有备份），动作照样给一个 `write_sidecar`——执行时再核一次，
  仍坏就备份并拒绝，每轮审计里看得见；修好之后下一轮照常写（人写的字段保留）。
- 健康报告 warn `sidecar_corrupt`：哪几部番、为什么要人修（坏着的这段时间里 `season_offsets` 读不到，
  判重 / 改名对"声明季对不上"的一律不认，是安全的一侧，但抓取会停）。
- 检测期仍然只读：备份只在执行阶段、写的那一刻做（`bug-fix/2026-09-26-diagnose-writes-sidecars.md`）。

## 测试

`tests/test_sidecar_ownership.py`：每个字段恰好一个归属；诊断之后人改的 `season_offsets` / `require_any` / `notes`
与不认识的键在 write_sidecar 之后都在（改之前被盖掉）；payload 里的旧用户字段不算数；已有 tmdb_id 不被改、没有才填；
`pinned` 的派生字段不动；别名取并集；目录本轮改名 → skipped；坏 JSON / 顶层不是对象 → `load_checked` 报问题；
坏 sidecar 被报出、备份一次、两轮都不被覆盖，修好后照常写；抓取不覆盖坏 sidecar；回退不覆盖之后坏掉的 sidecar；
`run` 的健康报告 warn `sidecar_corrupt`。`tests/test_effect_recheck.py` 的两个写 sidecar 出错的用例改成往
`write_text_atomic` 注入。
