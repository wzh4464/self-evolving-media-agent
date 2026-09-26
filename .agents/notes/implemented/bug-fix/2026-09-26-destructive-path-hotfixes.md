# 删改路径的安全热修：回退、隔离、死种、qBittorrent 失联

**日期**: 2026-09-26
**状态**: implemented / bug-fix
**触发**: 整改前的全库只读测绘（critic N1–N3、N5、N7；testinfra B1–B3）。
每一条都先在离线基座上把现场搭出来、看它失败，再修。

这份笔记按修复逐条追加，每条对应一个提交。

## 1. 回退前校验逆操作参数（critic N1 / LAT-02）

**症状（复现于 scratchpad，未在生产触发）**：`restore_from_trash` 写的是
`Path(u.get("trash_path") or "")`。空串变成 `Path('.')`——真值、而且"存在"——
于是 `shutil.move('.', dst)`。`os.rename('.')` 报 EINVAL，shutil 退回
`copytree(cwd → 媒体库)` 再 `rmtree(cwd)`：当前目录被整个拷进一个叫
`… S01E12.mp4` 的目录，然后清空。异常被回退的逐条 `except` 吞掉，回退继续跑下一条。

**暴露面**：生产审计里 6 条 trash 记录的 `trashed_to` 是 null——
20260830T132317（extras）、20260908T022758（dead-torrent ×3）、
20260908T143348 与 20260920T170126（duplicate-episode）。手动 rollback 的 cwd
就是项目目录（`.venv`、`.env`、`state/` 里的审计与 14GB 隔离区）。

**同类路径**：`rename_show_dir` 的逆操作对空 `path` 会把 cwd 里的每一项搬进
`cwd/<旧名>`；`restore_sidecar` / `ungrab_episode` 对空 `show_dir` 读写 cwd 里的
sidecar；`repair` 按审计里的空 `path` 在 cwd 里 `_merge_tree`；`relink` /
`recategorize` / `remove_tags` 对空 hash 静默"成功"。

**修法**：`Executor._undo_problem` 在任何逆操作动手前核对输入——
路径必须非空、绝对、已规范化（不含 `.`/`..`）、落在允许的根之下且不是根本身
（隔离区文件在 `state/trash` 下，其余在媒体库下）；文件名必须是单个路径分量；
种子内相对路径不得越出根目录；hash、magnet、bangumi_id 不得为空。
不合法就跳过并写明原因，绝不"尽力而为"。`restore_from_trash` 另外拒绝把目录
整体搬回；逆改名拒绝改目录；`repair` 用同一道闸筛审计记录。

**测试**：`tests/test_rollback_guards.py`——合成一条 `trash_path=""` 的生产形态
记录，在一个装着 `.env` / `state/audit.jsonl` 的假 cwd 里回退，断言 cwd 与媒体库
一个字节都没动；并对每个逆操作逐一喂坏参数。

## 2. `_op_trash`：先核对文件，再碰种子；种子那一步失败就停手（N1 根因）

**症状**：`_op_trash` 先 `qbit.delete`，再看 `path.exists()`。scan 会为"种子声明了、
盘上没有"的文件产出条目（`on_disk=None`，大小用声明值），路径也可能在诊断与执行
之间被挪走——这两种情况下种子记录丢了、文件一个没搬、审计写 `trashed_to: null`
和一条 `trash_path: ""` 的逆操作，也就是第 1 条那 6 颗定时炸弹的来源。
生产实例：20260920T170126 删掉朱音落语 S01E12 所属种子 d08f05a7 的记录，`freed 0`。

另外两处"只记日志照样往下走"：`qbit.delete` / `set_file_priority` 失败时文件照搬
（种子还要这个文件，下一次校验就重新下回来）；`file_only` 时 `_torrent_rel_path`
认不出条目就静默不设优先级、照搬文件。

**修法**（顺序即安全性）：
1. 路径必须在媒体库之下、`lexists`、是普通文件。不存在 → `skipped`（种子与文件都不动）；
   是目录或不是普通文件 → `failed`（这是检测器违约，要进 `find_failure_patterns`）。
   目录一律拒绝：死种 content_path 对 NoSubfolder 多文件种子就是整个 Season 目录，
   且目录的 `st_size` 让体积配额形同虚设。
2. 配额、dry-run。
3. 处理种子：`files()` / `delete` / `set_file_priority` 任何一步失败 → `failed`，
   其余一切保持原样。`file_only` 按**完整相对路径**认条目（不再按文件名），
   认不出或不唯一就拒绝。
4. 搬文件；失败时 `failed` 记录里写明 `torrent_record_lost` / `priority_zeroed`。
   于是 `applied` 的 trash 记录永远带着一个真实的 `trash_path`。

**测试**：`tests/test_trash_guards.py`——幻影路径、诊断后被挪走、目录、媒体库外、
delete / set_file_priority / files 各自失败、条目认不出、搬运失败；每个都断言
qBittorrent 与磁盘的快照不变（或如实记录）。

## 3. 死种只摘它自己的记录，按停滞时长判死（testinfra B1 / critic N7）

**症状（潜伏，未在生产触发）**：`DeadTorrentDetector` 的动作是
`trash{path: content_path}`。NoSubfolder 多文件种子（本项目 `add_torrent` 默认的布局，
生产 10 个）的 content_path 就是整个 `Season N` 目录——一个死掉的
`[TV版&无修版] 尼古喵喵 - EP11` 会把整季、12 个种子的文件、所有封存集位搬进隔离区，
目录 `st_size` 只有几百字节，体积配额拦不住。同一目录两个死种的 finding 因
`key()=(kind, path)` 相同塌成一条。判死用 `now - added_on`：量的是加入多久，
不是停滞多久。它也不分地方：2026-09-08 删了 `Media/.staging/opm-oad/` 下三个
手动暂存的种子。

**事实**：生产 3 次死种处置全是 `freed 0`——content_path 不带 `.!qB` 后缀，
从来只摘了记录，半成品留在盘上。上一条修复之后，旧检测器对单文件死种会被跳过
（路径不存在）、对 NoSubfolder 会被拒绝（目录），所以本条必须同时改动作。

**修法**：
- 动作改为 `drop_torrent{dead: True}`：只摘种子记录（magnet 可回退），磁盘一个字节
  不动。finding 按 hash 出（`path` 为空），不再与同目录的其它死种撞键。
- 判死：`is_dead_now`（没下完、无做种、availability ≤ 0）且
  `now - last_sign_of_life ≥ DEAD_TORRENT_HOURS`，后者取 `added_on` /
  `last_activity` / `seen_complete` 中最晚的。两个字段按 WebAPI 文档是 Unix 时间戳，
  **未在生产逐条实测**，所以取 max：缺失或异常值自动退回 `added_on`，不会比以前激进。
- 只管媒体库番剧目录里的种子（与 scan 同口径，`.` 开头的一级目录不算）。
- 有已下完的成员文件就只报告、不带动作：那是可播的正片，还在做种。
- 执行器 `drop_torrent` 的 dead 分支执行前活体复核：仍然 `is_dead_now`、仍无已完成文件。
- 逆操作 `readd_torrent` 记下 `no_subfolder`（`root_path` 为空即是），按原布局加回，
  半成品才对得上；旧记录没有该字段时保持原来的 False。

**遗留**：死种的 `.!qB` 半成品仍留在盘上（与生产一直以来的实际效果相同），
清理它们需要"只搬本种子自己的、未被别的种子认领的文件"，留到统一删除闸门那一期。

**测试**：`tests/test_dead_torrent.py`——NoSubfolder 季目录里一个死种不连累兄弟
（且 `converge` 收敛）、同目录两个死种都处理、老但近期活跃的不算死、刚见过完整
副本的不算死、`.staging` 与库外种子不管、有已完成成员只报告、执行前复核、回退
保持布局、48h 阈值按 last_activity 计。

## 4. qBittorrent 不可用或读不全时整轮 fail closed（critic N2 / N3 / LAT-01）

**症状（生产已发生）**：`build_context` 登录失败只打一行警告，`ctx.qbit=None`
照样执行。扫描看到 0 个种子，每个有种子的文件都成了"纯本地文件"：改名走被禁止的
文件系统分支，隔离跳过种子处理。2026-09-19 run 20260919T225410（登录超时）把
`朱音落语/Season 1/朱音落语 S01E12.mp4` 以 `torrent_hash ""` 移进隔离区——它归种子
d08f05a7；下一轮 20260920T170126 又把那个种子的记录删了（`freed 0`）。
离线复现一字不差：同一现场 qBit 断开后，执行器 applied 了一条无 hash 的 trash
和两条 `via: filesystem` 的改名。

更隐蔽的一半：`scan._torrent_files` 把任何 `files()` 错误缓存成 `[]`，那个种子的
文件就无声地变成无主文件。每轮约 539 次调用，一次 WebUI 超时就够。
`rollback` / `repair` / `purge --apply` 也不看 qBit：逆改名退化成 `Path.rename`，
`repair` 用 `_merge_tree` 搬活种子的文件，purge 丢掉种子证据后判定偏松。

**修法**：
- `build_state` 把 qBit 不可用、`torrents()` 失败、任一 `files()` 失败都记进
  `LibraryState.qbit_errors`（`ctx.qbit_errors` 指向同一列表）；`files()` 失败不再
  缓存成空列表；`torrents()` 失败不再让扫描崩掉——照常扫完磁盘，诊断仍可看。
- `Executor.qbit_blocker()`：qBit 为 None 或 `qbit_errors` 非空 → `apply` 整批拒绝
  （`ExecReport.refused`，不逐条写审计——一个都没尝试）；`rollback` / `repair` 同样拒绝，
  且拒绝时**不写** rollback 汇总记录（否则 `list_runs` 会把这批标成已回退）。
- 纵深防御：`_op_rename` / `_op_trash` 带 hash 而 qBit 不在 → 跳过；逆改名在种子里
  找不到文件时不再退化成文件系统改名（AGENTS.md 第 3 条对回退同样成立）。
- CLI：`run` / `apply` / `rollback` / `repair` / `purge --apply` / `evolve` 被拒时
  stdout 与 stderr 各打一行 `⛔ 拒绝执行任何改动：…`，退出码 `EXIT_DEGRADED=3`
  （以前 `run` 永远返回 0，launchd 看不出异常）。降级的 `run` 不修、不演进、
  不做隔离区的时间清理；apply 之后的演进重扫若读不全，跳过演进并以 3 退出。
  `scan` / `diagnose` 打一行"数据不完整，结论不可作为改动依据"。

**遗留**：`torrents()` **成功但返回残缺列表**识别不了——需要与上一轮的种子数做合理性
比对，而那要一份跨轮持久化的健康记录与阈值策略（合法的大批量删种不能永久卡死后续轮次），
归入下一期的每轮健康摘要。返回**空**列表的情形见第 12 条。

**测试**：`tests/test_qbit_fail_closed.py`——对照组确认现场会产生改名与隔离；
qBit 断开、单个 `files()` 超时、`torrents()` 超时三种形态下整轮快照不变；
回退 / repair 拒绝；逆改名不退化；CLI 各命令的退出码与提示；演进重扫残缺时跳过演进。

## 5. 同一批次里已处置的东西，后面的动作要认得出来（testinfra B2 / LAT-05）

**症状（生产已发生）**：诊断一次性全量产出，执行按动作顺序排——trash（op 5）在
rename（op 6）之前。判重把发布名输家整种子作废后，unrenamed-file 对同一个输家
提的改名去问已删种子的 `files()`，404 记成 `failed`：20260909T024309、
20260911T091146 ×2、20260916T102034、20260918T145907，并被 `find_failure_patterns`
当成"规则本身有问题"。另一半：纯本地的输家被搬走后，它的改名先查"目标名被占"
（赢家刚改好名），报一条误导的「集位被占」（20260924T173911 / T234117）。
第 2 条修复之后还多了一种：同批里合集的一集整种子作废、再对它只作废 NCOP，
`files()` 404 → `failed`。

**修法**：执行器记下本批次移除的种子（trash 整种子作废、drop_torrent）与搬进隔离区的
路径。`_op_rename` 最先核对这两样与"本地文件已不在"，都记 `skipped` 并写明原因；
`files()` 404（本批次之外被删）同样 `skipped`；种子说已下完、盘上连 `.!qB` 都没有的
幻影不改名（下载中、盘上还没有的照改——那是支持的功能）。同批对已移除种子的
第二次 trash 不再碰 qBit，只搬文件，丢记录只算一次。

**测试**：`tests/test_same_batch.py`——B2 原样现场（一轮后无 failed，赢家拿到集位名）、
本地输家不报「集位被占」、同一合集两次 trash、幻影不改名、0% 下载中仍改名。
基座元测试原先用 404 造 failed 记录，改用超时。

## 6. 每次扫描开头清缓存，同 ctx 重扫不再看到改名前的幻影（testinfra B3）

**症状**：`ctx._tfile_cache` 在 Context 的整个生命周期里从不失效。`cmd_run` 在 apply
之后用同一个 ctx 重扫给演进器用：改名前的条目名从缓存里出来，成了"种子声明了、
盘上没有"的幻影，改名后的真文件没被任何种子覆盖、成了本地文件——同一集两份
（幻影重复）外加一条"未改名"。离线复现：一次 renameFile 之后同 ctx 重扫，
扫描结果里同时有发布名路径与 `尼古喵喵 S01E08.mkv`。今天它只喂演进器；任何在同一
ctx 上循环 诊断→执行 的改造都会继承它，幻影被 trash 就是第 1、2 条那种记录。
模块级的 `builtin._OFFSET_CACHE`（sidecar 的 `season_offsets`）同样从不失效：
用户照提示补上换算后，同一进程里的下一次扫描仍按旧值报冲突。

**修法**：`build_state` 开头重置 `ctx._tfile_cache` 并清空 `_OFFSET_CACHE`——
缓存只活到这次扫描为止（它们本来就只为"单次扫描内别重复请求"而设）。

**测试**：`tests/test_rescan_freshness.py`——renameFile 后同 ctx 重扫只剩规范名、
无重复与未改名；两次扫描之间改 `season_offsets`，第二次按新值改名为 S01E58。

## 7. 演进规则带的动作一律不自动执行（critic N5）

**风险（潜伏）**：演进器的提示词允许 `trash` / `retag` / `recategorize` / `relocate` /
`write_nfo`，参数由模型选；`RuleSpec.detect` 用 `setdefault` 填 `path`，模型自带的
`path` 会保留下来。影子验证只在上线那一刻跑一次，此后不再复核。一条
`retag ma:SxxExx` 会成为 `_resolve`、封存、改名到钉子、`_inflight` 的权威依据——
未经人审的"LLM → 改名 / 判重删除"通路。离线复现：一条带 `trash` 且自带
`path=<Season 目录>` 的规则，走生产注册表（内置 + 演进）直接被执行（这次被第 2 条的
目录闸拦下，换成文件路径就是真删）。

**修法**：`RuleSpec.detect` 在 `evidence["origin"]` 写死 `"dsl"`（解释器写的，规则
JSON 改不了；`source` 字段却是 JSON 自己声明的，模型写 `builtin` 也能冒充）。
`Executor._dispatch` 见到它就跳过，理由「演进规则未经人工确认，不自动执行其动作」。
生产上 30 条演进规则 `action` 全是 null，这道闸今天行为中立；无动作规则照常出
finding、照常算"已解释"。人工确认后放行的正式流程留到后面的阶段。

**测试**：`tests/test_evolved_actions_gated.py`——规则 JSON 落在 RULES_DIR、经
`cli.build_registry()` 加载，trash（含自带 path）/ retag / recategorize / rename
都被跳过、库快照不变；JSON 自称 builtin 也拦得住；无动作规则不受影响。

## 8. 目录级搬运（改名回退、repair）先问活的种子视图，setLocation 失败就停手

**症状（审查复现，未在生产触发）**：第 4 条只在 qBit 不可用时拒绝回退与 repair；
qBit 在线时，两条目录级搬运仍会用文件系统搬走活种子的文件——正是 AGENTS.md 第 3 条
说的死链来源。

- `rename_show_dir` 的逆操作：`setLocation` 的异常被 `except Exception: continue`
  吞掉，随后把新目录**顶层**每一项 `shutil.move` 回旧目录——连同没搬成的那个种子的
  文件；改名之后才落进新目录的种子（此后抓的新集）根本不在 `torrent_savepaths` 里，
  文件同样被搬走。这条记录照样算 `reverted`、`failed=0`。后一种不需要任何故障：
  生产上有 78 条已执行的 rename_show_dir，回退其中较早的任意一条都会命中。
  顶层搬运还有一个老毛病：qBit 已经建好旧目录下的 `Season 1` 时，新目录 `Season 1`
  里的无主残留整个被跳过（与 `_merge_tree` 当年修掉的是同一个非递归 bug）。
- `repair_split_dirs`：同样吞掉 `setLocation` 的异常再 `_merge_tree`；只给 save_path
  在旧目录下的种子发 setLocation，content 在旧目录下、save_path 在更上层的种子
  （Original 布局、根目录恰好叫剧名）被 `_merge_tree` 直接搬走。另外 setLocation 在
  dry-run 判断之前就发出去了，`repair --dry-run` 也会真搬种子。

**修法**：
- `Executor._live_claims_under(root)`：问活的 `torrents()` + `files()`，返回在 root 下
  有文件的种子与它们声明的全部路径（含 `.!qB`、含优先级 0 的条目——setLocation 会一起搬）。
  判"在 root 下"不只看 save_path：content_path 或任一文件条目落在 root 下都算。
- `_merge_tree(old, new, skip=…)`：`skip` 里的路径一个都不碰。
- 目录改名的逆操作：改名后的目录已不存在（此后又改过名）→ 跳过，不再照记录把种子从
  不知道在哪的地方逐个 setLocation 回去（与逆改名"当前文件不存在"同口径）；
  活视图里有不在 `torrent_savepaths` 的种子 → 整条跳过并写明是哪几个
  （dry-run 同样报出来）；记录里、此刻仍在目录里的种子逐个 setLocation，任何一个失败 →
  抛出，记 `failed`，残留一个不动；都成功才按文件合并无主残留，绕开所有被声明的路径
  （setLocation 是异步的，qBit 可能还没搬完）。
- repair：同一个 claim 索引；setLocation 失败的那一对不合并、结果里带 `error`，
  `media-agent repair` 打 ❌ 并以 1 退出；被声明的路径不合并（`left_for_torrents` 计数，
  旧目录因此不会被清掉，交给人）；dry-run 不再发任何 setLocation。

**测试**：`tests/test_show_dir_moves.py`——回退用真实正向操作写下的审计记录：
回退时 setLocation 超时（failed、快照不变）、改名后落进来的种子（skipped、快照不变、
dry-run 同样报）、异步 setLocation 期间残留合并不碰被声明的文件；repair：setLocation
失败不合并、根在媒体根的 Original 种子不被合并、dry-run 零调用、同前缀兄弟目录不受牵连、
CLI 退出码。

## 9. 幻影输家摘记录、幻影不能赢判重、改名不改到别的种子声明的路径上

**症状（审查复现；是第 2 条修复带来的回归）**：LAT-01 之后朱音落语 S01E12 的种子
d08f05a7 成了幻影——`torrents/files` 报着 `朱音落语 S01E12.mp4`、进度 100%，盘上没有。
用户经 Jellyfin 删文件、手工挪文件也会造出幻影。第 2 条让 `_op_trash` 对不存在的路径
"跳过、种子与文件都不动"，于是同一批里：幻影输家 P 的种子留着，`_op_rename` 只看
盘上 `target.exists()`，把赢家 K 经 renameFile 改到 P 仍在声明的名字上——**两个种子
宣称同一路径**，此后每轮无声无息（scan 每个路径只出一条、colliding-torrent 不管两个都
100% 的）。main 在同一轮会摘掉 P 的记录（只是写了一条空 `trash_path` 的坏逆操作）。

复现时发现另一半更糟：P 声明得更大、发布名更好时，它在 `_rank_for_keep` 里**赢**——
探测不到文件就退回名字与声明大小——真文件 K 被当输家移进隔离区，这一集从库里消失，
幻影永远留着。钉了 `ma:` 的幻影同理会被封存：`meets_requirements` 探测不到时只看发布名。

**修法**：
- `builtin.is_phantom(f)`：有种子、盘上连 `.!qB` 都没有。判重只收已下完的，进桶的
  "盘上没有"就是幻影。判重里幻影不参与封存、排序永远在真文件之后；整桶都是幻影就只报
  `phantom_only`、不出动作；幻影输家的 trash 动作带 `phantom: True`。
- `_op_trash` 路径不存在时交给 `_trash_absent`：只有带 `phantom` 标记、且执行时复核
  （种子还在、条目仍按完整路径声明着它、进度 100%、优先级非 0、没有 `.!qB`）仍是幻影的，
  才处置种子一侧——整种子摘记录（`delete_files=False`，逆操作 `readd_torrent`，
  与 `drop_torrent` 共用 `_readd_undo`），合集里的一个条目（`file_only` 且还有别的
  要下的条目）则只设为不下载（新逆操作 `restore_file_priority`）。**绝不**写
  `restore_from_trash`。没有标记的（诊断之后才挪走的——那种多半该 relink）照旧跳过。
- `_op_rename`：盘上不在之外，再问活的 `torrents/files`：目标路径还被另一个种子
  （优先级非 0 的条目）声明着就跳过，理由写明是哪个种子（`_claimants`）。本地文件改名同样适用。

**遗留**（第 2 阶段已接上）：critic N6 说的"集位占用"要成为所有写路径的共用闸门——
`rename_single_video`（抓取后的即时改名）、`relink_torrent`、逆改名、`readd_torrent` 当时还没接上
`_claimants`。第 2 阶段把它换成共用原语 `media_agent/claims.py`、接到了全部写路径上（`_claimants`
已删），见 `architecture/2026-09-26-path-claims.md`。

**测试**：`tests/test_phantom_slot.py`——审查原样现场收敛后只剩 K 一个声明、隔离区为空；
幻影名字更好 / 钉了 `ma:` 时真文件都不进隔离区；只跑改名规则时，有种子与本地文件都不会
改到幻影声明的名字上；幻影摘除可回退；执行时复核（又开始下载、诊断后改过名）；合集里的
幻影条目只设优先级 0 且可回退。

## 10. 还在下载的特典立刻设为不下载（第 2 条带来的回归）

**症状（审查复现）**：ExtrasDetector 不排除没下完的条目——scan 来源 1 给的是干净的目标名，
盘上只有 `.!qB` 或什么都没有——照样提 `trash{file_only}`。main 上 `_op_trash` 先把优先级
设成 0，qBittorrent 就不再下它；第 2 条之后"路径不存在 → 跳过"把这一步也跳过了：特典照下
（带宽、94% 满的容器，critic N8），下完下一轮才隔离；停滞的合集每轮写一条理由是「种子声明了
但盘上没有，或诊断后被挪走」的误导跳过。单文件 PV / CM 种子在 main 上立刻被摘
（生产 20260830T132317 那一条）。

**修法**：`_trash_absent` 对 `file_only` 且条目进度 < 1 的：种子里还有别的要下的条目 →
`_zero_priority`（逆操作 `restore_file_priority`）；只剩它一个 → `_drop_record`
（逆操作 `readd_torrent`），半成品 `.!qB` 留在原地，与 main 相同。下完的特典照旧设为
不下载 + 移入隔离区。已下完却不在盘上、又没有 `phantom` 标记的，仍按"诊断后被挪走"跳过。

**测试**：`tests/test_extras_in_progress.py`——下载中的合集（有半成品 / 0% 没落盘）立刻
设为不下载、正片半成品不动；`converge` 一轮收敛、不再每轮跳过；单文件 PV 摘记录；
优先级可回退；下完的特典行为不变。

## 11. 抓取的换源放行与死种判据同一口径（第 3 条带来的回归）

**症状（审查复现）**：`grab._inflight` 把"已有种子在下"的集排除在抓取之外，例外是停滞
太久的——放行换源。main 上放行与判死都是 `now - added_on > DEAD_TORRENT_HOURS`，
新源抓取（op 0）与旧种子被摘（op 1）同一轮发生。第 3 条把死种改按"最后一次活着"计时，
`_inflight` 仍用 `added_on`：一个 72 小时前加入、10 小时前还收过数据的种子被放行换源，
新种子被 `_rename_grabbed` 改到同一个集位名上，旧种子却不算死、不被摘——两个种子
抢一个文件，colliding-torrent 只报「谁也完不成」不动手；旧种子只要还在给别人传分片，
`last_activity` 就一直刷新，永远等不到被摘。

**修法**：`builtin.droppable_dead(ctx, t, now)`——"这一轮 dead-torrent 会不会摘掉它"，
与检测器共用 `is_dead_now` / `_library_show_of` / `last_sign_of_life` /
`completed_members`（读不到文件列表 = 不知道 = 不放行；有已下完成员的死种检测器只报告，
这里也不放行）。`_inflight(ctx, show, by_hash)` 只对它放行。

**测试**：`tests/test_grab_stale_bypass.py`——审查原样（72h 加入、10h 前活动）不换源；
真死的换源且同轮摘掉旧种子（第 2 阶段起新种子不再被改到旧种子仍声明的集位名上，见 `architecture/2026-09-26-path-claims.md` 第 4 节）；刚见过完整副本的不换源；有已下完成员的
死合集不换源。

## 12. 登录成功却 0 个种子、而库里有视频：按数据不完整处理

**症状（审查复现，低严重度）**：第 4 条的闸门只在 qBit 为 None 或读取抛异常时触发。
`torrents()` 成功返回 `[]` 时，每个有种子的文件都成了 `torrent_hash ""` 的本地文件：
改名走文件系统、隔离跳过种子——与 LAT-01（20260919T225410）同一形态，只是没有任何报错。
离线复现：`[Group] Akane-banashi - 13 [1080p].mp4` 被 `via: filesystem` 改成
`朱音落语 S01E13.mp4`，qBit 仍声明着原名。

**触发面**：上游 qBittorrent 5.0 的 `application.cpp` 只在 `Session::restored` 的回调里
创建 WebUI，"启动中返回空列表"不会发生；剩下的是一个端着空会话的 qBit——比如容器重建时
没挂上 config / BT_backup 卷。

**修法**：`build_state` 在 `torrents()` 真的成功、返回空、而库里有视频文件时，往
`qbit_errors` 记一条"qBittorrent 报告 0 个种子，而媒体库里有 N 个视频文件"，执行器照第 4 条
整轮拒绝。不需要跨轮状态。库里确实一个种子都不用的，`.env` 设 `QBIT_ALLOW_EMPTY=1`
（`Config.qbit_allow_empty`）。测试里只有本地文件的库同样要显式 `lib.configure(qbit_allow_empty=True)`。

**测试**：`tests/test_qbit_fail_closed.py`——空会话 + 有种子的现场整轮拒绝、快照不变；
空库不受影响；覆盖开关放行；环境变量解析。
