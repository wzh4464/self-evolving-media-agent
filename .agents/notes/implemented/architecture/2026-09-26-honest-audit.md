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
