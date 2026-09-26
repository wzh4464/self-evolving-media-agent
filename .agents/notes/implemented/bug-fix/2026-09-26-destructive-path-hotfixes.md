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

**遗留**：`torrents()` **成功但返回空或残缺列表**（qBit 刚启动）目前识别不了。
需要与上一轮的种子数做合理性比对，而那要一份跨轮持久化的健康记录与阈值策略
（合法的大批量删种不能永久卡死后续轮次），归入下一期的每轮健康摘要。

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
