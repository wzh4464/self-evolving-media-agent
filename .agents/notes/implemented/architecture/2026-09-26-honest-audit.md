# 执行器不说谎、不丢记录：审计状态契约（整改第 3 阶段，逐节追加）

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 第 3 阶段"可观测"的第一件事：`audit.jsonl` 是回退、隔离区处置、失败模式统计的唯一依据，
而执行器会（1）把"其实已经生效"的改动记成 `failed` 且不带逆操作——生产 2026-09-14 run
20260914T100214 `Enticing.Circuit…mkv → 二十世纪电气目录 S01E11.mkv` 的 `renameFile` 读超时，
qBittorrent 其实已经改了，记录却是 failed、没有 undo，此后那个集位一直"被占"；（2）写审计本身出错
（磁盘满、权限、`json` 序列化不了）时让异常冲出 `apply()`，整轮连隔离区处置一起中止（critic N8 的余项）；
（3）回退只写一条汇总，逐步做了什么没有记录（critic N12）。

这份笔记按提交逐节追加；最后一节是完整的状态契约。

## 1. qBittorrent 的 404 按状态码认

**现场**：三处代码各自写 `"404" in str(e)` 认"种子已不在"——`claims._is_404`（占用索引把它当作不声明
任何东西）、`_op_rename`（记 skipped「所属种子已不在」）、回退的 `_restore_priority`。错误文本里只要
碰巧出现 404 三个字符（响应体里一段 hash、端口号、别的报错），一次**读失败**就被当成**状态变化**：
占用查询本该 fail closed（看不全就拒绝），却放行了。

**修法**：`QBitError(message, status=...)`，`QBitClient._get/_post/add_torrent/_login` 都带上 HTTP 状态码；
文本一字不改（日志、审计里的 `error` 照旧）。判断统一走 `clients.is_not_found(e)`：只认
`QBitError.status == 404`，没有状态码的（超时、连接错误、老式构造）一律不算——把读失败当成
"种子没了"，比反过来危险得多。FakeQbit 的错误同样带状态码。

**测试**：`tests/test_qbit_http_status.py`——真 `QBitClient` 走 `httpx.MockTransport`（不联网）核对状态码与
文本；`files()` 报 HTTP 500、响应体里有 "404" 时改名记 failed「无法确认」、一个字不改（以前记 skipped
「所属种子已不在」）。

## 2. 写审计永不抛异常，写不进去也不丢

**现场**（critic N8 的余项）：隔离区与媒体在同一个约 94% 满的 APFS 容器里，真到磁盘满的那一轮，最先写不进去的
就是 `state/audit.jsonl`。以前 `_audit` 是 `open("a").write(json.dumps(rec))`：

- 写不进去抛 OSError——而 `_audit` 总在改动**之后**调用：改名 / 隔离做了，记录没有；
- `apply()` 的 `except` 接住它，再写一条 failed——又抛，这次冲出 `apply()`，`run` 连隔离区处置
  （磁盘满时唯一腾空间的一步）一起中止；
- `args` / `extra` 里混进一个 `Path`，`json.dumps` 抛 TypeError，同样的连锁；
- 上一个进程写到一半死了（末尾半行、没有换行），下一条直接粘在后面，读的人把两行一起跳过。

**修法**：新模块 `media_agent/audit.py`。

- `audit.write(audit_log, rec)` 永不抛，返回问题列表：序列化不了的值按 `str` 降级写进主审计并标
  `audit_degraded`（`Path` 降级成路径字符串，回退照样能用）；主审计写不进去就把**同一行**原样打到
  stderr（前缀 `[audit-fallback]`，launchd 下进 run.err.log），并尽力追加到同目录的
  `audit.fallback.jsonl`；备用文件也写不进去也说出来。
- `append_line` 追加前看文件末尾是不是换行，不是就先补一个——半行自成一行坏 JSON，下一条完好。
- `_audit` 先把记录放进本轮报告，再写盘；问题记进 `ExecReport.audit_problems`，`summary()` 带上条数。
  `apply()` 的 except 用 `_describe(e)`，连 `str(e)` 出错都不抛。
- 每条执行器记录多了 `seq`（本批次内从 1 起的序号）。同一批的记录可能分在两个文件里，文件顺序不再是
  写入顺序；`_read_audit` 按 `seq` 排，回退的 LIFO 靠它。旧记录没有 `seq`，保持文件顺序。
- **读的一方两个文件一起读**（`audit.iter_records`）：`rollback` / `runs` / `repair`（`_read_audit`、
  `list_runs`）、隔离区处置（`purge._audit_by_trash_path`）、`find_failure_patterns`。备用文件里的一条
  若主审计里也有（写到一半报错、其实写进去了），按 `(run_id, seq, ts, op, status)` 只认主审计那条；
  同一个文件里从不去重。读不了的文件当作空、在 stderr 说一句，不抛。
- 回退的汇总记录同样走 `audit.write`，问题放进结果的 `audit_problems`。
- cli：`apply` / `run` / `rollback` 在输出末尾与 stderr 各说一遍，退出码 `EXIT_AUDIT_INCOMPLETE = 4`
  （与"整批拒绝、什么都没改"的 3 分开）。`run` 照样跑完隔离区处置再报。
- **问题带种类，按落点措辞**（第 3 阶段复审时补）：`write` 返回的是 `audit.Problem`（`str` 的子类，调用方照旧当字符串
  用），`kind` 是 `DEGRADED`（按字符串写进了主审计）/ `FALLBACK`（转写进了 stderr 与备用文件）/ `STDERR_ONLY`（只剩
  stderr）；执行器与回退拼"哪一条"前缀用 `prefixed`，种类不丢。cli 的大字、`ExecReport.summary()`、健康报告的
  `audit_incomplete` 原因（报告里多了 `actions.audit_problem_kinds`）、通知邮件都用 `audit.where(audit.kinds(…))` 说这几条
  落在了哪。以前一律说"已转写到 stderr 与 audit.fallback.jsonl"——序列化降级的那条其实在 audit.jsonl 里、备用文件根本
  不存在，照着去找的人扑空。降级仍算 critical / 退出码 4：值没能原样记下（多半是代码把 `Path` / `set` 塞进了审计），
  这条的回退未必可靠。

**测试**：`tests/test_audit_robustness.py`——ENOSPC 注入在 `audit.append_line`：批次继续、这一条在 stderr
与备用文件里各一份、照样能回退；动作抛异常 + 审计一直写不进去不冲出 `apply()`；`Path` 混进 `args` 降级写入
并能回退；末尾半行不吞下一条；`cmd_run` 跑到隔离区处置并以 4 退出。tripwire 新种类 `audit_fallback`：
测试里任何一条审计没原样写进 audit.jsonl 都会变红，除非声明。三种落点各自的措辞：降级的不提备用文件与转写（cli、
`summary()`、健康原因、通知各一个断言；改之前 3 个红）。

## 3. 新状态 `unknown`：改动也许生效了、确认不了

**现场**：`failed` 在执行器里一直兼着两种意思——"没生效"与"异常发生在改动之后，不知道生效没有"。
生产 2026-09-16 … 09-26 的 12 次抓取（20260916T041844 … 20260926T055214）在 `add_torrent` 成功之后撞上
`resp` NameError，全记 failed、没有 undo，qBittorrent 里其实多了种子；2026-09-14 run 20260914T100214 的
`renameFile` 读超时，qBittorrent 已经改了名，同样记 failed、没有 undo，此后那个集位一直"被占"。

**修法（这一节是通用兜底，按动作核实见下一节）**：

- `apply()` 期间把 `ctx.qbit` / `ctx.abdb` 包一层 `_Tracked`：每个写方法（`_QBIT_WRITES`、`abdb.write`）
  **发出即记**进 `self._effects`（抛了异常也可能已经生效）；文件系统上的改动由动作自己 `_effect()`。
  每个动作开始时清空；结束后还原 Context 上的客户端。
- 动作抛了异常、自己没接住（`_crashed`）：没发出过改动 → `failed`；发出过 → `unknown`，带
  `effects_attempted`、`reason`，以及动作事先用 `_intend(undo)` 登记的"如果生效了该怎么撤"。
- 报告分桶多一个 `unknown`（critic §3.5：原来是 `{...}[status]`，新状态直接 KeyError，而这一行就在改动之后）；
  不认识的状态进 unknown 而不是 failed。`summary()` 带"未确认 N 项"。

**向后兼容地引进**：生产 audit.jsonl 2026-09-26 只读核对——9627 行、状态只有 applied / skipped / failed /
rollback；574 行（2026-08-17 上午）没有 `run_id` 也没有 `undo`；4 条回退汇总只有 `ts` / `run_id` / `status`
与计数，`run_id` 是被回退的那一批。每个读状态的地方：

| 读的一方 | 对 `unknown` |
|---|---|
| `rollback` | 带 `undo` 的与 applied 一起按 LIFO 尝试（每个逆操作动手前都核对此刻状态，没生效的自然跳过）；结果多 `unconfirmed` / `unconfirmed_reverted` / `unconfirmed_no_undo`，cli 单独一行标出，明细里标「当初未确认」 |
| `list_runs` / `runs` | 多 `unconfirmed` 计数；带 `undo` 的算进 `undoable`（`rollback --last` 因此会选到它）；输出「❓未确认 N」 |
| `repair` | unknown 的 `rename_show_dir`（种子搬了、残留搬到一半出错）同样是分裂现场；半路中止的（`rename_show_dir_partial`，unknown 或 failed）也是，repair 往前做完 |
| 隔离区处置 | unknown 的 trash **不算已隔离**：那份文件交给人，理由写明「隔离未确认」，永不自动删（`purge._trash_records`） |
| `find_failure_patterns` | failed 与 unknown 分开计数，结果多 `status`；`evolve` 输出标「失败 / 未确认」 |
| cli `apply` / `run` | 逐条列出 ❓ 未确认的动作 |

不认识的状态（将来的）一律当作"不是已生效"：不计入回退、不计入已隔离、不计入失败模式。

**测试**：`tests/test_audit_contract.py`——按生产几代格式合成的 audit.jsonl（没有 run_id 的、秒级 run_id、
回退汇总、预演、坏行、非对象行、带 seq 的 unknown、将来的状态）喂给 `list_runs` / `runs` /
`rollback --last` / `find_failure_patterns` / `_read_audit`；抓取 `add_torrent` 之后 NameError 记 unknown、
之前抛异常仍是 failed；回退带逆操作的 unknown（生效了的还原、没生效的不动）；unknown 的隔离交给人；
unknown 的目录改名进 `repair`。tripwire 新种类 `unknown_record`。

## 4. 改动调用出错之后按此刻状态核实（`_settle`）：改名、标签、分类

**现场**：生产 2026-09-14 run 20260914T100214，`Enticing.Circuit…mkv → 二十世纪电气目录 S01E11.mkv` 的
`renameFile` 读超时——qBittorrent 其实改了名。审计记 failed、没有逆操作：这次改名回退不了，那个集位此后
一直"被占"，2026-09-20 起每轮一条「集位被占」。qBittorrent WebUI 的超时（2026-09-19/20 又三次）常常就发生在
它处理完请求之后。

**修法**：`Executor._settle(f, a, err, probe, what=, undo=, extra=)`——改动调用抛了异常，问一次此刻的状态：

| `probe()` | 记什么 |
|---|---|
| True：生效了 | 照常走完、记 `applied`，带逆操作；那个异常进 `confirmed_after_error` |
| False：与动手前一致 | `failed`，`effect` 写明"按此刻状态核实：没有生效"，不带逆操作 |
| None：对不上（部分生效 / 被别人同时改了） | `unknown`，带"如果生效了该怎么撤" |
| 自己抛异常：读不到 | `unknown`，`reason` 写明复核也失败 |

这一节接上的核实（都读**此刻**的 qBittorrent，不走占用索引的缓存）：

- 改名（`renameFile`）：种子条目里是新名字 / 原名（`_rename_landed`）；纯本地文件的 `Path.rename`：
  盘上源 / 目标谁在（`_fs_moved`）。**原名还列着不等于没生效**（复审时补，qBittorrent 5.2.3 源码核过）：
  `TorrentImpl::renameFile` → `doRenameFile` 只是 `++m_renameCount` 并调 libtorrent 的 `rename_file`，排进磁盘队列；
  `torrents/files` 返回的 `m_filePaths` 要等 `file_renamed` 告警回来、在 `handleFileRenamed` 里才改。磁盘队列积压
  （卷 94% 满、正在下载 / 校验）时，刚受理的改名在列表里还是原名。所以只有**请求没被受理**——连不上
  （`ConnectError` / `ConnectTimeout`）或当场被拒（`QBitError` 带状态码：409 名字冲突等，在排队之前判）——时原名
  才是 False；读超时、断连之类先按 `_RENAME_SETTLE_S`（0.3 s、1 s）再看两次，落地了照常 applied，还是原名就是 None
  （unknown，带逆改名；回退时逆改名自己核对此刻的名字）。以前一律 False：记 failed、不带逆操作，还写着"核实过没
  生效"——改名随后落地，正是 2026-09-14 那种回退不了的记录。setLocation（`moving`）与删除之前就按源码核过，改名
  这时才补上。relink 的逐条改名同一个判据。
- `retag`：标签都在 / 都不在（`_tags_landed`）；`recategorize`：分类是新的 / 原来的（`_category_landed`）。
- `relocate`（只有演进规则会产出，分派处已拦）：`save_path` 是目标、或 `state == moving`（qBittorrent 5.2.3
  对有元数据的种子排异步搬运，`save_path` 搬完才变，`moving` 在处理请求时就置上——与正常返回同一口径）。

**测试**：`tests/test_effect_recheck.py`。FakeQbit 新增 `fail(..., after=True)`：改动照常生效之后再抛
（"响应丢了"）；默认仍是"请求没到"。生产那次改名原样：记 applied、带逆操作、能回退；请求没到：failed；
改完之后 qBittorrent 读不到：unknown、带逆操作，回退照样还原；状态对不上：unknown。异步改名（复审时补）：FakeQbit
的 `rename_lag` 让受理了的改名晚几次 `files()` 才看得见（以前同步改名，"原名还在 = 没生效"的误判测不出来）——
受理了、慢一拍：applied（改之前 failed）；连不上 / 409：failed；读超时而原名一直在：unknown、带逆改名（改之前 failed）；
relink 的逐条改名慢一拍照样算重建成功（改之前整条 failed）。

## 5. 删除一侧的核实：摘种子、设为不下载、搬进隔离区

**现场**：`_op_trash` 整种子作废时 `delete` 超时，以前一律记「删除种子记录失败，文件未动」并停手——而超时时
种子常常已经摘掉了（qBittorrent 5.2.3 的 `SessionImpl::removeTorrent` 在处理请求时就把它从列表里 `take`
掉）：记录说种子还在，文件也没进隔离区，下一轮扫描把它当成没有种子的纯本地文件。`drop_torrent`、幻影 / 下载中
特典的 `_drop_record` / `_zero_priority` 同理。搬进隔离区（`shutil.move`）出错则一律 failed。

**修法**：

- `delete` 出错：按种子列表认（`_torrent_gone`）。不在了 → 照常往下走（隔离接着搬文件、摘除记 applied 带
  magnet 重加的逆操作）；还在 → failed；读不到 → unknown（摘除带"重加"的逆操作；隔离写明 `path_still_at`，
  文件不动）。说不清时**不**记进 `_removed_torrents`——那样占用索引会无视它的条目，后面的改名可能改到它仍声明的路径上。
- `filePrio` 出错：按那个条目此刻的优先级认（`_priority_landed`），unknown 带恢复优先级的逆操作。
- 搬进隔离区出错（`_settle_move`）：跨卷搬运是先拷后删、**删源是最后一步**——
  原位置没了且隔离区那份大小对得上 → 搬完了，applied；原位置还在 → 没搬走，failed，隔离区里若多了半份，
  写进 `stray_copy`；其余（原位置没了、那份大小不对或也不在）→ unknown，带 `restore_from_trash` 逆操作
  （能捞回来的只剩那一份）。failed / unknown 都写明种子那一步已经发生的事（`torrent_record_lost` /
  `priority_zeroed`）。
- 隔离区处置（`purge._trash_records`）：unknown 的隔离与 failed 留下的 `stray_copy` 都**不算已隔离**，那份文件
  交给人、理由写明（「隔离未确认」/「搬运失败留下的拷贝」），不再是"来历不明"。

保留下来的旧口径：各动作原来的报错开头（「删除种子记录失败，文件未动：」「设为不下载失败，…」「搬入隔离区失败：」）
照旧放在 `error` 前面（`_settle(prefix=)`），按它们检索日志的习惯不断。

**测试**：`tests/test_effect_recheck.py` 的"隔离 / 摘种子 / 设为不下载"一节，每种核实结局各一个现场。

## 6. setLocation 的核实：重新关联、目录改名；sidecar

- `relink_torrent`：`setLocation` 出错按 `_location_landed` 认；每个映射的 `renameFile` 出错按
  `_rename_landed` 认，改成了照算 `relinked`，说不清的有一个就整条记 unknown（`unconfirmed` 计数），逆操作
  是"已改的 + 说不清的"整体反过来（逆操作逐条改名、改不动的跳过）。一个映射都没改成而目录已经挪了：
  仍是 failed，但写明 `relocated_from` / `relocated_to`（以前这件事不留痕迹）。触发校验（`recheck`）的请求
  出错**不再**让整条变成失败：映射已经改好了，那是这个动作的改动本身——记 applied、带逆操作，
  `recheck_triggered: false` + `recheck_error`（以前抛出去记 failed、没有逆操作：映射改了，却回退不了）。
- `rename_show_dir`：每个种子的 `setLocation` 出错按 `_location_landed` 认，受理了照算已搬。确认没搬的仍是
  failed；有说不清的就是 unknown（半迁移、还不知道迁了多少）。两者都记 `rename_show_dir_partial`——
  回退现在对它明说"只做了一部分、无法自动回退，交给人 / `repair --run <批次>`"，不再报"未知逆操作"。
  **repair 真的接得住**（复审时补）：最初的部分逆操作只有 `moved_hashes` / `torrent_savepaths`，没有新旧目录，
  `repair_split_dirs` 又只认 `rename_show_dir`——照着建议跑 `repair --run X` 得到「分裂目录 0 对」、退出码 0，
  两个目录各一半，最需要人动手的时候给了一句假安慰。现在部分逆操作也带 `path` / `new_name` / `bangumi_id` /
  `prev_savepath`，repair 认它（unknown 与 failed 都认——确认有种子没搬成的同样是分裂现场），把旧目录里剩下的
  种子交给 qBittorrent 搬过去、合并残留，最后补上改名没走到的一步：AutoBangumi 的 save_path 改到新目录
  （`_repair_ab_savepath`；改不成在 repair 的输出里说、退出码 1）。往前做完而不是往回撤：旧目录那一半的种子
  没搬过、搬它们与第一次改名是同一个动作，回撤却要把已搬的再搬一遍。种子都搬完之后
  搬残留（`_merge_tree`）或改 AB 数据库时抛异常：`_intend` 事先登记了完整的 `rename_show_dir` 逆操作，
  通用兜底记 unknown 并带上它；逆操作按此刻状态核对（旧目录还在就拒绝），`repair` 认得这条 unknown、
  把残留合并过去。
- `write_sidecar`：`sidecar.save` 先写临时文件再原子替换，抛了异常而内容没变 → failed（核实过的）；变了 → 说不清。
  `write_nfo` 登记文件系统改动（没有逆操作，出错时至少不说成"没生效"）。

**测试**（repair 接部分改名，复审时补）：`tests/test_effect_recheck.py`——照着回退的建议跑 repair：unknown 的部分改名
合并成一个目录、两个种子都在新目录（改之前 `pairs == 0`）；failed 的部分改名 repair 搬剩下的并把 AB 的 save_path
改到新目录；AB 改不成时 `repair` 命令说出来、退出码 1。

**测试陷阱（本节测试踩过一次）**：测试里撤销自己的替身不要用 `monkeypatch.undo()`——它连 conftest 的隔离
（`PROJECT_ROOT` 打到临时目录）一起撤掉，之后的回退把汇总记录写进了仓库自己的 `state/audit.jsonl`
（本机开发目录，已删；`tests/test_purge_log.py` 里早有同样的提醒）。只 `setattr` 回原值。

## 7. AutoBangumi 数据库与抓取的核实

- `fix_title_aliases` / `repoint_rss` 的 `abdb.write` 是"docker stop → 改库提交 → docker start（`check=True`）"。
  容器起不来时 CalledProcessError 在**提交之后**抛出——以前记 failed、没有逆操作，库其实改了、回退也找不到它，
  AutoBangumi 还停着没人知道。现在出错后读库（只读连接，容器停着也能读）：值是新的 → applied、带逆操作，
  并写 `after_error_note`（"容器可能还停着，需人工确认"），日志一行，这一轮健康报告 warn
  `ab_container_maybe_stopped`（第 3 阶段复审时补——以前只有日志与这个字段，这一轮照样 ok、不发信）；值是原来的
  （`docker stop` 就失败了、或事务没提交）→ failed；读不到 / 对不上 → unknown 带逆操作（`Executor._ab_write`）。
- `grab_episode`：
  - `add_torrent` 出错：按 .torrent 算出的 v1 infohash 查种子在不在。在 → 照常走完（即时改名、写 have），
    `already_present: null`（分不清是这次加的还是本来就有）；不在 → failed（沿用「加种子失败: 」开头）；
    读不到 / 纯 v2 种子算不出 infohash → unknown，写 `infohash`，**不**写 have、不带逆操作（没有"撤掉抓取"的逆操作——
    回退的 `ungrab_episode` 从来只动 have）。
  - 种子加进去之后写 sidecar 出错：种子已加入是抓取的改动本身，记 **applied**（带 `ungrab_episode`）并写
    `sidecar_error` / `sidecar_note`；这一集照样记进 `_grabbed`——本轮排在最后的 `write_sidecar` 会把它并进
    have，没有那一条就下一轮 409 那条路径补。以前整条记 failed、`_grabbed` 也没记，同一批的 `write_sidecar`
    拿诊断期的旧快照把 have 盖回去（生产 2026-09-16 起 12 次抓取丢记账是同一类：改动之后的异常吞掉了记账）。

**测试**：harness 新增 `lib.docker_fail("start" | "stop")`——docker 替身在那个子命令上记完日志以 1 退出。

## 8. 回退逐步留审计

**现场**（critic N12）：`rollback` 只写一条汇总（`status: rollback` + 计数）。还原了哪几条、跳过了哪几条、半路
在哪一步出错，审计里都查不到；健康摘要看不见回退改了什么。汇总的字面量写着 `"run_id": "rollback-of-<id>"`，
却被后面的 `**result` 覆盖回原批次号（critic §2 的更正：executor 调研以为 `list_runs` 会把标记打在
`rollback-of-X` 上，其实标对了；生产上 4 条历史汇总也都是原批次号）。

**修法**：

- 每一步逆操作一条记录（`_write_step`，形状见 `audit` 模块文档）：`run_id` = `rollback-of-<X>`、
  `rollback_of` = X、`rollback_id` = 这次回退自己的 ID、`op` = `undo:<逆操作>`、`args` = 逆操作、`undoes` 指回
  原记录；`status` 还原了 applied / 核对后跳过 skipped（`reason`）/ 没动就出错 failed / 动了之后出错 unknown
  （回退期间同样用 `_tracking()` 记下发出的写调用；逆操作里的文件系统搬运也 `_effect()`）。预演不写。
- 汇总照旧、并且明写：`run_id` 是 X、`status` 是 `rollback`，另加 `rollback_run_id`、`rollback_id`。历史汇总
  （只有 `ts` / `run_id` / `status` 与计数）照样标记它们回退的批次。结果里的 `failed` 仍是"这一步抛了异常"
  （含 unknown 的步），cli 与既有测试的口径不变；精确的状态在逐步记录里。
- `list_runs`：回退批次带 `rollback_of`，没有可回退的条目；被回退的批次由汇总**或**逐步记录标"已回退"
  （汇总那一行写不进去时也标得上）。`runs` 输出「↩回退 X 的记录」；`rollback --last` 不选回退批次；
  `rollback --run rollback-of-X` 明确拒绝（回退记录不带逆操作，回退不能再回退），退出码 3。
- `_read_audit` 先按 `rollback_id` 分组再按 `seq` 排：同一批可以回退不止一次，每次的 `seq` 各自从头数。
- **逆操作自己接住的异常也要说实话**（复审时补）：`readd_torrent` 的逆操作接住 `add_torrent` 的异常、返回
  "跳过"，逐步记录写 skipped——可加种请求超时常常是 qBittorrent 处理完了、响应没回来（前向抓取修的就是这个形态），
  种子也许已经回来了。现在按 magnet 的 infohash 核实：在 → 还原了（applied）；不在 → 确实没改，照旧 skipped；
  读不到 → 往上抛，按"发出过改动之后出错"记 unknown、`effects_attempted: ["qbit.add_torrent"]`。其余逆操作返回
  "跳过"的都在发出改动之前（核对此刻状态、占用、参数）。

**不做的**：回退的逐步记录不带"再做一遍"的逆操作。逆操作的逆（`restore_from_trash` 的逆是再隔离一次、
`readd_torrent` 的逆是再摘掉）各自要过删除关口与占用检查，不是把记录反过来就行——需要时另起一个阶段。

**测试**：`tests/test_rollback_steps.py`。重新加种：超时但加上了 → applied（改之前 skipped）、请求没被处理 → skipped、
超时之后读不到 qBittorrent → unknown（改之前 skipped）。

## 9. 部署前的状态备份带上 audit.fallback.jsonl

`deploy.sh` 切换前把小体量状态复制到 `state/backups/<时间>-<版本>/` 供手工比对（代码回滚从不回卷审计）。
`audit.fallback.jsonl` 是审计的一部分（回退、`runs`、隔离区处置一起读它），同样复制；文件不存在就跳过，
与 `deploy.history` 同一写法。

## 10. 完整的状态契约（给以后读 / 写审计的人）

权威文本在 `media_agent/audit.py` 的模块文档；这里是同一份的展开，带上每种记录的字段。

**执行器的动作记录**（`Executor._audit`，一个 finding 恰好一条）：

| 字段 | 何时有 | 说明 |
|---|---|---|
| `ts` `run_id` `status` `dry_run` `rule` `kind` `op` `args` `summary` | 总有 | 与第 2 阶段之前相同。`run_id` 形如 `YYYYMMDDTHHMMSS.mmm-<pid>`（旧的是秒级） |
| `seq` | 第 3 阶段起总有 | 本批次内从 1 起的序号；回退按它排 LIFO（旧记录没有，按文件顺序） |
| `undo` | 可逆且生效了 / 也许生效了 | 逆操作的完整描述，`rollback` 按它还原 |
| `reason` | skipped、unknown | 为什么没动 / 为什么确认不了 |
| `error` | failed、unknown | 异常（`类型: 文本`），可带动作原来的报错开头 |
| `effect` | 核实过的 failed | "…出错；按此刻状态核实：没有生效" |
| `effects_attempted` | unknown | 已经发出的改动，如 `["qbit.add_torrent"]`、`["qbit.set_location", "fs.merge_tree"]` |
| `confirmed_after_error` | 核实过的 applied | 改动调用报了错、按此刻状态核实确实生效——那个错 |
| `audit_degraded` | 序列化降级时 | 原记录里有序列化不了的值，按 `str` 写入 |

四种状态：

| `status` | 意思 | 改没改东西 | 带 `undo` | 回退 |
|---|---|---|---|---|
| `applied` | 生效了，已确认 | 改了 | 可逆就带 | 尝试 |
| `skipped` | 没动手 | 没改 | 不带 | 不管 |
| `failed` | 没生效（异常在任何改动之前，或核实过没生效） | 意图中的改动没发生；已发生的附带改动以字段写明（`torrent_record_lost` / `priority_zeroed` / `stray_copy` / `relocated_from`…） | 一般不带（目录改名半路中止带 `rename_show_dir_partial`，回退明说交给人 / `repair`，repair 往前做完） | 不管 |
| `unknown` | 也许生效了、确认不了 | 可能改了、可能改了一部分 | 有"如果生效了该怎么撤"就带 | 尝试（每个逆操作动手前核对此刻状态），结果单独计数、标「当初未确认」 |

**回退**：逐步记录（`run_id` = `rollback-of-<X>`，`rollback_of`、`rollback_id`、`op` = `undo:<逆操作>`、
`args` = 逆操作、`undoes`、四种状态同上、可带 `notes`，**不带** `undo`）+ 一条汇总（`status` = `rollback`，
`run_id` = X，`rollback_run_id`、`rollback_id`、各计数、`audit_problems`）。

**文件**：`state/audit.jsonl` 为主；写不进去的原样转写到 stderr 与 `state/audit.fallback.jsonl`。读的一方一律用
`audit.iter_records`（两个文件、坏行跳过、备用文件里与主文件重复的去掉）。

**读的一方的规矩**：不认识的状态当作"不是已生效"——不计入回退、不计入已隔离、不计入失败模式；新增状态时
在 `audit.py` 里加常量、在这张表里加一行、在 `tests/test_audit_contract.py` 的合成审计里加一代。

**写的一方的规矩**（新加动作照此办）：动手前 `_intend(逆操作)`；qBittorrent / AB 数据库的写调用自动记进
`effects_attempted`，文件系统上的改动自己 `_effect("fs.…")`；能按此刻状态核实的改动调用出错时走
`_settle(probe=…)`，不要直接记 failed。
