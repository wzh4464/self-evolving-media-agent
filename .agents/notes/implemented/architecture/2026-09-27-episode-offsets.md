# 集号偏移：sidecar 的 `episode_offsets`，按库内季、一处口径

**日期**: 2026-09-27
**状态**: implemented / architecture
**触发**: 整改第 5 阶段（单一所有者）前的准备：AutoBangumi 退役之前，它今天提供的每一样都要先由 media-agent 接住。
critic N13：抓取对 AB 带 `episode_offset` 的番无能为力、而且不出声（生产只影响一条有效订阅：AB 37《超超超超超喜欢你的
100个女朋友》第三季，`-24`——这一季的发布按连续集号编，`- 25` 是 S03E01）。

## 以前的样子

集号偏移只活在 AB 库的订阅行上（`show.bangumi["episode_offset"]`），三处各算各的：

| 读的人 | 怎么用 | 问题 |
|---|---|---|
| `builtin._slot_from`（判重 / 改名 / purge 的发布名复核） | 整部番一个值 | Season 1 里的 `- 05` 也减 24，换算成 -19、认不出 |
| `kernel.have_episodes`（抓取、订阅健康的"已有"） | 整部番一个值 | 同上 |
| `builtin.ledger_view`（出处账本从番组页标题重算） | 只对 AB 订阅的那一季（`_ab_offset_for`） | 只认 AB 行上的 `season`，不看 `season_offset` |
| 抓取（`grab.py`） | 不看 | N13 |

而且 AB 一退役（订阅行删掉、或在 AB 里停用成 `deleted=1`——`AutoBangumiDB.bangumi` 只读 `deleted=0`），换算无声地没了：
判重、改名、`have` 都按 25 认。

## 现在

**sidecar 新字段 `episode_offsets`**（人的意图，`sidecar.USER_INTENT`）：`{"3": -24}` = 库里第 3 季的原始集号加 -24。

- **键是库内季号**（文件落在哪个 `Season N`），这一季的原始集号一律加。与 `season_offsets` 是两回事：那边的键是发布方
  **声明的**季号、`集号 <= 偏移` 才加（TMDB 压平成一季、发布方仍按分季编号）；这边是 AB `episode_offset` 的语义
  （整条订阅一个值、只对它订阅的那一季、不管发布声明了什么——AB `manager/renamer.py::gen_path`）。两种语义合进一个字段
  会让"键是哪种季号"说不清，所以分开。
- **只换算原始集号**：文件名里已经是 `SxxEyy` 的不动（那是换算过的结果，`naming.apply_episode_offset`）；番组页标题
  从来不是改出来的名字，`S03E25` 也照换（与 AB 一致）。
- **换算出非正数 = 认不出**（`naming.offset_episode`）：`第三季 - 01` 配上 `-24`。AB 这时悄悄退回原始集号；这里不猜——
  一季里两种编号混着来的发布说不清是哪一集。判重不收、改名不提、抓取把它报成"换算不进这一季"（下一篇提交）。
  沿用 2026-09-26 的决定（`tests/test_episode_offset.py::test_offset_that_would_give_a_non_positive_episode_is_unparsable`）。
- **登记了就算数**：`{"3": 0}` = 这一季不换算，盖过 AB 订阅行上的偏移。

**一处口径 `builtin.episode_offset_for(show, season)`**：sidecar 登记了这一季就按它；没登记时退回 AB 订阅行上的
`episode_offset`，**只对 AB 下载落进的那一季**（`abrow.library_season`：`save_path` 的 `Season N`，没有就是
`season + season_offset`，不到 1 退回 `season`——AB `downloader/path.py::_gen_save_path`）。判重（`_slot_from`，
`naming.release_slot` 接受按季取偏移的函数）、出处账本（`ledger_view` → `title_slot`）、`have`（`kernel.episode_of_file`
同样接受函数）都问它。sidecar 里写坏的项（不是数、季号不是数字、布尔）按没写认，不让判重崩。

`media_agent/abrow.py`：AB 订阅行上要读的几样（库内季、番目录名、番组页 id、偏移），只读。

## 对生产的影响

- 只有 AB 37 一条有偏移。它的 Season 1 / 2 里要是还有没改名的 `- NN` 发布名文件，以前认不出（-24 之后非正），现在
  按 S01ENN / S02ENN 认——正确的方向。
- 迁移（把 AB 行上的偏移写进 sidecar）是另一个动作，另作一步；在那之前规则照旧从 AB 行上读，那一季的行为不变。
- 抓取还不看偏移（N13 的后一半），另作一步。

## 测试

`tests/test_episode_offsets_sidecar.py`：只有 sidecar 的偏移（没有 AB 行）判重 / `have` / 改名目标一致是 S03E01；sidecar 的
`{"3": 0}` 盖过 AB 的 -24；AB 的偏移只对它那一季（Season 1 的 `- 05` 以前认不出）；AB 的 `season_offset` 把偏移挪到
`Season <season + season_offset>`；AB 行停用之后 sidecar 照样换算；出处账本按同一个口径从番组页标题重算（以前没有 AB 行
就算成 (3, 25)、要改名成 S03E25）；写坏的项不崩。`tests/test_episode_offset.py` 原有的 5 条照旧通过。
