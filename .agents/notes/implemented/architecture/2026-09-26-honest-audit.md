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

**测试**：`tests/test_audit_robustness.py`——ENOSPC 注入在 `audit.append_line`：批次继续、这一条在 stderr
与备用文件里各一份、照样能回退；动作抛异常 + 审计一直写不进去不冲出 `apply()`；`Path` 混进 `args` 降级写入
并能回退；末尾半行不吞下一条；`cmd_run` 跑到隔离区处置并以 4 退出。tripwire 新种类 `audit_fallback`：
测试里任何一条审计没原样写进 audit.jsonl 都会变红，除非声明。

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
| `repair` | unknown 的 `rename_show_dir`（种子搬了、残留搬到一半出错）同样是分裂现场 |
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
  盘上源 / 目标谁在（`_fs_moved`）。
- `retag`：标签都在 / 都不在（`_tags_landed`）；`recategorize`：分类是新的 / 原来的（`_category_landed`）。
- `relocate`（只有演进规则会产出，分派处已拦）：`save_path` 是目标、或 `state == moving`（qBittorrent 5.2.3
  对有元数据的种子排异步搬运，`save_path` 搬完才变，`moving` 在处理请求时就置上——与正常返回同一口径）。

**测试**：`tests/test_effect_recheck.py`。FakeQbit 新增 `fail(..., after=True)`：改动照常生效之后再抛
（"响应丢了"）；默认仍是"请求没到"。生产那次改名原样：记 applied、带逆操作、能回退；请求没到：failed；
改完之后 qBittorrent 读不到：unknown、带逆操作，回退照样还原；状态对不上：unknown。
