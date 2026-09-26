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

## 测试

`tests/test_subscriptions.py`：只有订阅档案的目录被扫描登记（不是电影、TMDB 按 sidecar 认）；没有订阅的空目录、坏档案的
空目录照旧不登记；订阅的季盘上一集都没有也抓；订阅的季与盘上的季一起看；订阅里的番组页先于搜索（搜到的别的页日期
对不上）；端到端（迭代）种子加进 `<番目录>/Season 1`、`have` 记上。改之前 5 条红（另两条是"照旧不登记"的对照）。
