# 订阅：盘上一集都还没有的季也抓（sidecar 的 `subscriptions`）

**日期**: 2026-09-27
**状态**: implemented / architecture
**触发**: 整改第 5 阶段（单一所有者）。critic §4：退役 AutoBangumi 的下载不能让新番、新季停下来。可 media-agent 的抓取
只看 sidecar `seasons` 里的季（`grab.py`：`if not sc.seasons: continue`、只迭代已有的季键），`seasons` 是 sidecar-sync 按
**盘上的文件**记的；扫描又只登记有文件的目录（`scan.build_state`）。新番、老番的新一季，在别的什么放进第一个文件之前
永远不会被抓——今天那个"别的什么"是 AB（ab 调研 §5.3："the biggest gap"）。

## `subscriptions`（人的意图，`sidecar.USER_INTENT`）

`{"3": {"source": "autobangumi", "bangumi_id": 37, "mikan_id": "3417", "since": "2026-09-27"}}` = 库里第 3 季要抓。

- **为什么不往 `seasons` 里加一个空的季键**：`seasons` 是派生的，sidecar-sync 每轮按盘上重算、写档案时以这一轮算的为准
  （`sidecar.merge_for_write`）。诊断期算的 payload 在同一轮里就会把刚加的季键盖掉（同一次迭代里先加键、后写档案），
  下一轮再加、再被盖——来回翻。订阅是人的决定（在 AB 里订的、`media-agent subscribe`、新一季开播时登记的），归人的意图，
  写档案永远不碰。
- `mikan_id`：这一季的番组页（AB 订阅 `rss_link` 里的 bangumiId、`subscribe --mikan`）。抓取把它排在候选的第一个
  （`_resolve_mikan_id(preferred=…)`），照样按播出日期打分——对不上日期的不用。
- `source` / `bangumi_id` / `since`：从哪来、何时登记的，给人看、给回退核对（逆操作按整条记录比对）。

## 扫描

只有订阅档案、还没有文件的番目录照样登记成一部番（`scan._subscribed`：sidecar 解析得了、`subscriptions` 非空），不是电影。
没有订阅的空目录（历史遗留、人删空了的）照旧不登记——不能因为这一步去抓用户删掉的番。TMDB 身份照旧：sidecar 有
`tmdb_id` 按它认，没有就按目录名 / AB 标题搜（搜到后 sidecar-sync 填进去，模型选的走 `pin_tmdb`）。

## 抓取

要看的季 = `seasons` 的季键 ∪ `subscriptions` 的季键。没有 `seasons` 条目的季，`have` 就是盘上的（空）。其余照旧：
`is_seasonal`（只补在播 / 刚播完的季）、`season_layout_mismatch`、偏好、日期闸、`MAX_PER_SHOW`。抓下来的第一集由抓取
自己写进 `seasons[N].have`，之后 sidecar-sync 按盘上接着记。

## 谁登记订阅

- AutoBangumi 的有效订阅（`ab-adoption`：`create_show_dir` / `subscribe_season`，见 `architecture/2026-09-27-ab-adoption.md`）；
  盘上已经有的季不登记（抓取本来就看它）。
- **开播的新一季**（`new-season`，`plugins/new_season.py` → `subscribe_season`，source `new-season`）：AB 的订阅一季一行，新
  一季要人在 AB 里再订一次；AB 退役之后没有这一步，老番的新一季就不会来。规则：
  - 这部番是**订阅着的**：sidecar 有 `subscriptions`，或 AB 里有它的有效订阅（`show.bangumi`）。档案里留着的旧 `bangumi_id`
    不算——那可能是人在 AB 里停用了的订阅，人不追了，不替他订。
  - 下一季 = 库里已知的最大季号（`seasons` 的季键、订阅、盘上下完的、AB 订阅的库内季）+ 1。它在 TMDB 上有定了档的集、第一集
    在 `NEW_SEASON_LEAD_DAYS`（7）天之内或已经播了、是在播的季（`is_seasonal`）→ 登记。
  - 分集表走 `cache.season_episodes`（6 小时；TMDB 上还没有这一季的 404 同样 6 小时内不再问），**不看扫描缓存的季列表**：
    那是按条目缓存 30 天的，新一季上了 TMDB 要等它过期才看得见。每轮每部订阅着的番最多问一次 TMDB。
  - **不登记**库内编号与 TMDB 对不上的：sidecar 有 `season_offsets`（压平 / 换算进来的番），或库里最大那一季的集数比 TMDB
    那一季还多（库里按连续编号）——TMDB 的"下一季"多半就是库里已有的后半段，登记了只会再抓一遍。要就自己在 sidecar 里写。
  - 要停：在 AB 里停用订阅，并删掉 sidecar 的 `subscriptions`。
- **人手的 `media-agent subscribe`**（`media_agent/subscribe.py`，source `cli`）：AB 不在、或不想经 AB 时。
  `--tmdb ID [--season N] [--mikan ID] [--dir NAME] [--require-any WORD …] [--offset N] [--dry-run]`：
  - 季省略 = TMDB 上最新的一季；TMDB 上还没有的季照样记下、提醒一句。
  - 目录：某个目录的 sidecar 记着这个 tmdb_id 就订进那个目录（不另建——两个目录抓同一部番，抓取只认文件多的）；有两个就要
    `--dir`；`--dir` 指向别的目录而这部番已有目录 → 拒绝。都没有就按 TMDB 标题（斜杠换全角）新建，大小写不同的同名目录算已有。
  - 写的是人的意图：`subscriptions[季] = {source: cli, since, mikan_id?}`、新目录的 `tmdb_id` + `tmdb_source: human`（已有
    目录只在还没有时补）、`mikan_id`、`require_any`、`episode_offsets[季]`。**经同一套动作与审计**（规则名 `subscribe`：新目录
    `create_show_dir`、已有目录 `subscribe_season`），能 `rollback`；qBittorrent 读不全照样整批拒绝（退出码 3）。
  - **不覆盖人写的**：已有目录的 sidecar 里 `tmdb_id` 不同、`require_any` 不同、这一季的 `episode_offsets` 不同 → 拒绝、什么都
    不写（退出码 2），要改就直接编辑 sidecar。这一季已经订阅 → 什么都不写，照样给预览。
  - 然后说出下一次抓取会做什么（`subscribe.preview`：扫描一遍、抓取检测器对这部番原样跑一遍，只读）：可抓的集、为什么不抓
    （找不到番组页、编号对不上、都没过硬门槛……）；**不在播的季说清楚不会抓**——抓取只补在播 / 刚播完的季（`is_seasonal`），
    老番的整季补档不在这一步。
  - 预演（`--dry-run` / `AUTO_APPLY=false`，与 `apply` 同一个口径）不写，也不给预览（订阅还没写，抓取看不到它）。
  - 拿运行锁（与 `run` / `grab` 排队），不看维护暂停（人手的命令，与 `rollback` / `repair` 一样）。

## 测试

`tests/test_subscriptions.py`：只有订阅档案的目录被扫描登记（不是电影、TMDB 按 sidecar 认）；没有订阅的空目录、坏档案的
空目录照旧不登记；订阅的季盘上一集都没有也抓；订阅的季与盘上的季一起看；订阅里的番组页先于搜索（搜到的别的页日期
对不上）；端到端（迭代）种子加进 `<番目录>/Season 1`、`have` 记上。改之前 5 条红（另两条是"照旧不登记"的对照）。

`tests/test_cli_subscribe.py`：新番建目录（经执行器，审计规则是 `subscribe`）、sidecar 里是人的意图、预览列出可抓的集；
季省略取 TMDB 上最新的；`--require-any` / `--offset` 写进去；已有这部番的目录订进那里、不另建；`--dir`；与人写的
`require_any` / `episode_offsets` / `tmdb_id` 冲突时拒绝、什么都不写；两个目录都记着它要 `--dir`；TMDB 查不到拒绝；预演不写；
已经订阅的什么都不写；不在播的季说清楚不会抓；`rollback` 撤掉建的目录；子命令接好、拿运行锁（被占 75）。

`tests/test_new_season.py`：AB 订阅着的番第二季三天前开播 → 登记（参数、来源、开播日）；一周内开播的登记、更晚的等着；只有
sidecar 订阅的也算；没人订着（档案里只剩旧 bangumi_id）的不登记；老早播完的"下一季"不算新；TMDB 上没有下一季时什么都不说；
已经登记的不再登记；`season_offsets`、库里一季比 TMDB 长的不登记；端到端（迭代）第一次迭代登记、下一次迭代就抓。
