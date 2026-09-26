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
- 目录还没有的随 `create_show_dir` 一起写（下一节）。坏档案不迁（sidecar-sync 报 `sidecar_corrupt`，执行时备份、拒绝）。

### 订阅本身 → 番目录与 `subscriptions`

AB 在的时候，是 AB 把第一个文件放进番目录，media-agent 的扫描与抓取才看得见这部番 / 这一季（ab 调研 §5.3："the
biggest gap"）。现在对每一条有效订阅（`subscriptions` 的语义见 `architecture/2026-09-27-subscriptions.md`）：

- **番目录还没有**（按 `claims.fold` 比，大小写不敏感）→ `create_show_dir`（`ab_subscription_new`，important，path = 番目录）：
  同一个番目录的几条订阅合成一个动作——建 `<媒体根>/<番名>/`、订阅季的 `Season N/`（也免得被扫描当成电影）、一份只有人的
  意图的 sidecar：`subscriptions`（每季 `{source: autobangumi, bangumi_id, mikan_id?}`）、`mikan_id`（`rss_link` 里的
  bangumiId，搜索式 RSS 没有）、AB 的集号偏移（`episode_offsets`，不再单独迁）。**这是订阅接手里唯一的媒体根写入。**
  TMDB 身份不在这里写：下一次迭代扫描按目录名 / AB 标题搜（原有的一套），sidecar-sync 填、模型选的走 `pin_tmdb`；抓取用
  扫描搜到的身份，同一轮 `run` 里就能开始抓。
- **番目录在、这一季盘上还一集都没有**（sidecar 的 `seasons`、`subscriptions` 都没有它，盘上也没有下完、认得出集位的——
  与 sidecar-sync 记 `seasons` 同一个口径）→ `subscribe_season`（`ab_subscription`，subject `Sxx`）。番组页 id 只在 sidecar
  还没有 `mikan_id` 时补。**盘上已经有的季不写**：抓取本来就看它，生产上 35 条订阅不因此多出 35 次写（部署后第一轮什么都不
  提议，ab 调研 §0：35 条都对得上有档案、有这一季的目录）。
- **认不出番目录**（`save_path` 里没有媒体根目录名、名字是隐藏 / 季目录）→ `ab_subscription_unmapped`（important，不动手）：
  media-agent 接不住这条订阅，AB 退役之后它就停了。
- **保存路径过期**：`save_path` 指向不存在的目录，而另一个番目录的档案记着这条订阅（sidecar-sync 写下的 `bangumi_id`）→
  `ab_subscription_moved`（important，不动手）。不建空壳：抓取会把它当重复目录跳过（`tmdb_groups`），标题对齐还要往已有的
  目录上改名。

`create_show_dir`（执行顺序 0）：只建一个此刻谁都没占着的新目录——`claims.check_dir`：盘上同名（大小写不敏感）的目录、
save_path 在它下面的种子（AB 刚加、还没元数据、盘上什么都没有的也算）都算占着，skipped 并写明占用者；看不全就 failed
（AGENTS.md 第 8 条）。逆操作 `remove_show_dir {path, created}`：目录里只剩它自己建的东西（sidecar、建的空 `Season N`、
Finder 的 `.DS_Store`）时才逐个删、逐层 rmdir，永不 `rmtree`；已经有别的（抓来的集、NFO、坏档案的备份）或有种子的文件 /
保存路径在它下面，整步跳过并写明。`subscribe_season`（执行顺序 10）经 `_set_intent`，逆操作 `unset_sidecar`。
`converge._TARGET`：`create_show_dir` 按 `show_dir`，`subscribe_season` 按 `(show_dir, season)`。

## 写法：往 sidecar 里**补**人的意图（`Executor._set_intent`）

`episode_offsets` 归人的意图（`sidecar.USER_INTENT`）：写档案（`write_sidecar`）从不写它。迁移是人在 AB 里写下的东西
**搬**过来，所以是一个专门的动作，契约比 `pin_tmdb`（身份，只在没有时填）再严一点：

- 只写还没有的那一项；先在副本上试，没得写就 skipped、不白写文件；写的那一刻（`sidecar.update` 读-改-写）再核一次；
- 逆操作 `unset_sidecar {show_dir, entries: [{field, key, value}]}` **只摘这一项、而且只在它还是写下的值时摘**：人后来改过
  （`-24` 改成 `-23`）整步跳过并写明；已经没了算已还原；别的字段、不认识的键都不动。以前的 sidecar 动作用
  `restore_sidecar`（整份还原）——对"补一项"来说太粗：回退一个星期前的迁移，会把这期间人改的、sidecar-sync 写的一起盖回去。
  能摘的字段是白名单（`actions._UNSETTABLE`：`episode_offsets` / `subscriptions` 按键，`mikan_id` / `require_any` /
  `tmdb_id` / `tmdb_source` 摘成空值），回退不能借它摘别的东西。动作带进来的 `intent` 先过 `actions._clean_intent`：只认
  这几个字段、形状要对。
- 预演、目录本批次已改名、坏档案、写出错按此刻核实（`_settle`）——与 `pin_tmdb` 同一套。
- `converge._TARGET`：`(show_dir, season)`；执行顺序与写档案同一档（10）。

## 测试

`tests/test_ab_subscriptions.py`（AB 库是真的 sqlite）：AB 刚订的新番同一轮 `run`（迭代）里建目录、登记订阅、下一次迭代按
番组页抓进 `Season 1`；同一个缺的番目录两条订阅合成一个动作；AB 的偏移随建目录写进去、之后不再单独迁；搜索式 RSS 没有
番组页 id；老番的新一季登记订阅（别的字段都在、逆操作只摘写下的两项）、再诊断不再提议；盘上已有的季不写；已有的
`mikan_id` 不改；认不出番目录的报 `ab_subscription_unmapped`、不建；保存路径过期的报 `ab_subscription_moved`、不建空壳；
大小写不同的已有目录不再建；AB 刚加的、盘上什么都没有的种子占着目录名时不建（写明占用者）；诊断与预演不建；回退删掉只剩
自己建的东西（含 `.DS_Store`）的目录、有了别的东西就不删；回退订阅只摘它、之后人写的留着。

`tests/test_ab_adoption.py`（AB 库是真的 sqlite）：sidecar 没有就提议、参数对；执行只写这一项（别的人写的字段、不认识的键
都在），逆操作形状对，再诊断不再提议；已有的（`{"3": 0}`）不提议；诊断之后人写上的执行时不覆盖；偏移为 0 / 停用的行不提议；
`season_offset` 决定迁进哪一季；目录还没有的随建目录一起写（诊断不建）；诊断与预演不写；坏档案执行时不覆盖；回退只摘这一项、人后来写的
别的留着；人后来改了这一项回退不动它；端到端：迁完之后在 AB 里停用订阅，`- 25` 仍是 S03E01。
