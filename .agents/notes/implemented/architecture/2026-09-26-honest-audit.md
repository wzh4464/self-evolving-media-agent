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
