# 接手 AutoBangumi 订阅：AB 知道、media-agent 不知道的东西先迁进 sidecar

**日期**: 2026-09-27
**状态**: implemented / architecture
**触发**: 整改第 5 阶段（单一所有者）。critic §4：用户**通过 AB 订阅**（`autobangumi-subscribe-verify` 流程，生产 35 条有效
订阅），退役 AB 下载不能让新番、新季停下来；AB 以后仍是订阅的前端。可 AB 库里有些东西只有它知道，AB 一停——或者订阅在
AB 里被停用成 `deleted=1`（`AutoBangumiDB.bangumi` 只读 `deleted=0`）——就无声地没了。**在关掉 AB 的任何东西之前**，先由
media-agent 接住。

## 规则 `ab-adoption`（`media_agent/plugins/adopt.py`）

读 AB 库的 `bangumi` 表（只读连接，`deleted=0` 的行都算有效——AB 自己的 `match_torrent` 也只跳过 `deleted`，`archived`
照样下载）。订阅行上要读的几样在 `media_agent/abrow.py`：落在哪个番目录（`save_path` 里媒体根目录名后面那一段，与扫描
同一个口径）、哪个库内季（`save_path` 的 `Season N`，没有就是 `season + season_offset`）、番组页 id（`rss_link` 里的
`bangumiId`）、集号偏移。

### 集号偏移 → `episode_offsets`（`ab_episode_offset`，important，subject `Sxx`）

订阅行上 `episode_offset` 非 0、番目录已经在、sidecar 这一季还没有登记 → 动作 `adopt_episode_offset`
`{show_dir, season, offset, bangumi_id}`。语义见 `architecture/2026-09-27-episode-offsets.md`；迁之前规则退回 AB 行，行为不变。

- 已经登记了（人写的、上一轮迁的，含 `{"3": 0}`）不提议；执行时按此刻的文件再核一次，诊断之后人写上的也算
  （skipped「已有 …（人写的为准）」）。
- 目录还没有的不在这一步迁（没有文件可换算）。坏档案不迁（sidecar-sync 报 `sidecar_corrupt`，执行时备份、拒绝）。

## 写法：往 sidecar 里**补**人的意图（`Executor._set_intent`）

`episode_offsets` 归人的意图（`sidecar.USER_INTENT`）：写档案（`write_sidecar`）从不写它。迁移是人在 AB 里写下的东西
**搬**过来，所以是一个专门的动作，契约比 `pin_tmdb`（身份，只在没有时填）再严一点：

- 只写还没有的那一项；先在副本上试，没得写就 skipped、不白写文件；写的那一刻（`sidecar.update` 读-改-写）再核一次；
- 逆操作 `unset_sidecar {show_dir, entries: [{field, key, value}]}` **只摘这一项、而且只在它还是写下的值时摘**：人后来改过
  （`-24` 改成 `-23`）整步跳过并写明；已经没了算已还原；别的字段、不认识的键都不动。以前的 sidecar 动作用
  `restore_sidecar`（整份还原）——对"补一项"来说太粗：回退一个星期前的迁移，会把这期间人改的、sidecar-sync 写的一起盖回去。
  能摘的字段是白名单（`actions._UNSETTABLE`），回退不能借它摘别的东西。
- 预演、目录本批次已改名、坏档案、写出错按此刻核实（`_settle`）——与 `pin_tmdb` 同一套。
- `converge._TARGET`：`(show_dir, season)`；执行顺序与写档案同一档（10）。

## 测试

`tests/test_ab_adoption.py`（AB 库是真的 sqlite）：sidecar 没有就提议、参数对；执行只写这一项（别的人写的字段、不认识的键
都在），逆操作形状对，再诊断不再提议；已有的（`{"3": 0}`）不提议；诊断之后人写上的执行时不覆盖；偏移为 0 / 停用的行不提议；
`season_offset` 决定迁进哪一季；目录还没有的不在这一步；诊断与预演不写；坏档案执行时不覆盖；回退只摘这一项、人后来写的
别的留着；人后来改了这一项回退不动它；端到端：迁完之后在 AB 里停用订阅，`- 25` 仍是 S03E01。
