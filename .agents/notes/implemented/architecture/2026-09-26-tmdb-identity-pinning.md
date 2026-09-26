# TMDB 身份钉住、按 id 缓存、标题稳定闸

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 整改第 4 阶段（身份与纯度）。两件事是同一个根：**每部番的 TMDB 身份每 30 天重新决定一次，而且没人看得见**。

- LAT-04（鬼物语）：`鬼物语/Season 1/` 的文件 2026-08-20 按 TMDB 46195 改成 `物语系列 S01E0x`；09-19 10:49
  （run 20260919T104905）按目录名缓存的条目过了 30 天、重新搜索没命中，标题退回目录名，文件被改回 `鬼物语 …`；
  16:51（run 20260919T165111）又搜到了，再改成 `物语系列 …`。09-16（run 20260916T223029）`终物语 下` 19 个、
  `续・终物语` 6 个文件在缓存过期、重新搜索选中了另一个条目之后被改成 `物语系列 …`。
- critic N4：扫描从不读 sidecar 里的 `tmdb_id`；缓存按目录名存，`rename_show_dir` 一改目录名就换了键、重新搜；
  多个候选时 `_pick_tmdb` 问模型——这是演进冻结管不到的第二条 LLM 通路，它的选择直接决定改名目标、目录名、分类，
  而且每轮重新做、不留记录。

## 身份（`scan._resolve_tmdb`）

1. sidecar 解析不了 → 身份认不准：不搜、不按任何标题改名（`naming_hold`），坏档案由 sidecar-sync 报。
2. sidecar 里有 `tmdb_id` → **照它认，不搜**。它是钉住的身份：代码只在没有时填一次（sidecar-sync 填搜到的，
   `tmdb_source=search`；模型选的经 `pin_tmdb`，`llm`），之后只有人改（字段归属见
   `architecture/2026-09-26-sidecar-ownership.md`）。生产上现有的 sidecar 都带着 sidecar-sync 写过的 `tmdb_id`——
   那就是库里此刻按它命名的身份，部署后原样钉住。
3. 没钉住 → 旧的按目录名缓存 → 搜目录名 / AB 标题 → 确定性规则（唯一结果、唯一精确同名）选中的这一轮就用，
   sidecar-sync 随后把它填进 sidecar。
4. 规则选不出、要问模型的 → **这一轮不用**：答案缓存（`tmdbpick:<目录>`，7 天，预演的轮次不重复问），记进
   `state.tmdb_proposals`；`tmdb-identity` 检测器报 `tmdb_pick`（important）并带 `pin_tmdb` 动作。执行时只在
   sidecar 还没有 tmdb_id 时写（`tmdb_id` + `tmdb_source=llm` + 标题），有审计、逆操作 `restore_sidecar`；
   已有的一律不改（记 skipped，写明现有的是谁）。下一轮起照钉住的认，不再搜、不再问。模型说不知道的，按搜不到负缓存。

## 缓存

- 条目元数据（标题、季）按 **tmdb_id** 存（`tmdbshow:<id>`，30 天）。一次 `tv_detail` 同时取标题与季（以前
  `official_title` 与 `seasons` 各打一次同一个接口）。部署后第一轮：旧的按目录名的条目 id 对得上就迁过来，不重查。
  按目录名的旧格式照写一份（回退到上一个版本时它还认）。
- 取不到新的（出错、或这一轮已经出过错）→ 用旧值兜底（`Cache.get_tmdb_stale`，不看时效）。
- 搜不到（或模型也选不出）→ 负缓存 `tmdbmiss:<查询词>`，`TMDB_MISS_TTL` = 24 小时。以前每轮重搜库里 40 多个
  本来就没有条目的目录，扫描白花 8–10 秒。
- 这一轮 TMDB 出过一次错（超时、连不上）→ 其余的不再打网络，全用缓存（一次 20 秒超时 × 几十部番）。

## 标题稳定闸（`media_agent/titles.py`，`state/titles.json`）

`Show.tmdb_title`（经 `official_title` 决定改名目标、`rename_show_dir` 的新名字、分类名、抓取后改名）取稳定后的标题：

| 情况 | 这一轮用 | 记录 |
|---|---|---|
| TMDB 给的 = 已采用的 | 它 | —— |
| 取不到（出错、没缓存） | **已采用的**——绝不退回目录名 / AB 标题（LAT-04 那一步） | —— |
| 新标题，第 1 轮看到 | 已采用的；报 `tmdb_title_pending`（minor） | 正在确认：标题、第几轮、哪一轮 |
| 新标题，**连续第 2 轮**看到（`CONFIRM_RUNS`） | 新的（改名、目录名、分类跟上） | 旧的记进"30 天内换掉的" |
| TMDB 给回 30 天内刚换掉的标题（`FLIP_WINDOW_DAYS`） | 已采用的；报 `tmdb_title_flip_blocked`（important） | —— |
| 人钉住（sidecar `pinned` 里有 `tmdb_title`） | sidecar 里的 `tmdb_title` | 不记 |
| 钉着 id、一个标题都不知道（没记录、sidecar 没写、没缓存、TMDB 取不到） | 空 → `naming_hold`，这部番这一轮不改名 / 目录名 / 分类 / NFO / 抓取；报 `naming_held` | —— |

- "连续"数的是记下来的轮次：只有 `run` 在扫描之后记（`titles.record`），`diagnose` / `apply` 只读——人手跑几次
  `diagnose` 不能把新标题"确认"下来。中间有一轮没看到（取不到、或看到的是别的）就重新数。这一轮没问 TMDB
  （`--no-tmdb`）不算一轮。一个 tmdb_id 一个决定（物语系列 14 个目录共用一个）。
- 没有记录时（部署后第一轮、记录读不了）以 sidecar 的 `tmdb_title`、再以旧缓存里的标题为"已采用"——库里此刻
  正在用的名字。记录读不了说一句、按没有记录处理；写不进去说一句（最坏是新标题多等一轮）。
- sidecar-sync 写进 sidecar 的 `tmdb_title` 是**采用的**，不是 TMDB 这一轮给的。

## 运维要知道的

- 部署后钉住的是 sidecar 里**此刻**的 `tmdb_id`——sidecar-sync 以前每次解析变了都跟着改，所以翻转过的番
  （LAT-04 里 09-16 那一轮的 `终物语 下`、`续・终物语`）钉住的可能是翻转之后的那个条目。部署前后核对一遍这几部番的
  `tmdb_id`，不对就直接改 sidecar（可写 `"tmdb_source": "human"`）；改了身份，下一轮起标题照常过稳定闸。
- 回退一次 `pin_tmdb` 只是把 sidecar 还原成没有 `tmdb_id`：模型的答案缓存 7 天，下一轮会再提议同一个。
  模型选错了，直接在 sidecar 里写对的 `tmdb_id`，比回退管用。
- 要改回一个 30 天内刚换掉的标题（`tmdb_title_flip_blocked`）：sidecar 里写 `tmdb_title`、`pinned` 里加 `"tmdb_title"`。

## 没做的

- `volumes` 组（物语系列）的文件仍按组标题命名（`物语系列 S01E0x`）：LAT-04 的另一条不变式"分卷的文件按自己的目录
  命名"要把 14 个目录的文件全改一遍，是一次性的大改名，留给人决定。
- 抓取每轮直接问 TMDB 分集表（不走缓存，约 32 秒）不在这一步。

## 测试

`tests/test_tmdb_identity.py`：LAT-04 现场（化物语 + 鬼物语 钉在 46195，缓存过期、TMDB 取不到 → 不改名、标题仍是
物语系列；改之前改回目录名）；钉住的不搜、两个目录一次 `tv_detail`；目录改名后不重查；旧的按目录名缓存迁移；
搜不到负缓存、过期再搜；新标题第 2 轮才采用（档案里也是）；中间断一轮重新数；30 天内不改回去、31 天后照常确认；
人钉的标题；`diagnose` 不推进计数；`cmd_run` 记录；一个标题都不知道 → 不改名；坏 sidecar → 不改名、不搜；
模型选的这一轮不用、钉进 sidecar、下一轮不再问；预演不重复问；钉住的番从不问模型；`pin_tmdb` 不改已有的 id；
稳定闸的单元测试与记录读不了。`tests/test_silent_excepts_report.py` 的注入点从 `official_title` 改成 `tv_detail`。
