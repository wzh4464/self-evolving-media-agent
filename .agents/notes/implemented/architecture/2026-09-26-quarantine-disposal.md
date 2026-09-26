# 隔离区处置：每一次硬删除都有记录、有依据、看容量

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: 整改前的只读测绘（2026-09-26）发现隔离区有两条硬删除出口，都不合格：

- `run` 末尾的 `Executor.purge_trash` 按日期 `rmtree` 整个隔离区日目录（超过
  `TRASH_RETENTION_DAYS=30` 天），不看替代者、不看规则、不写任何记录。生产 run.log 里两次：
  「清理 4 个过期文件，释放 3.0GB」「清理 2 个过期文件，释放 1.7GB」——删的是哪 6 个文件只能倒推
  （很可能是药屋 `[Tokuten]` 与 K-ON `[SP04] Cast Interview`）。它与 `purge.py` 自己的前提
  「不按时间清」、与 `feature/2026-09-04-safe-quarantine-purge.md`「没有接进自动轮次」正面矛盾；
  隔离区里 `purge` 明确拒绝的 8 个文件（手工 version_swap 的 7 个、合并发布的 1 个）会在 2026-10-14
  之后的第一轮被照删。
- `purge --apply` 先 unlink 后写日志；替代者按文件名正则认；没有最短隔离期；判重之外的都不管。

另一面（critic N8）：隔离区与媒体在同一个 APFS 容器（约 94% 满，剩约 97 GiB），**隔离不腾空间**，
只有硬删除才腾。时间清理不能简单删掉，得换成一个按处置类别、有记录、看容量的处置器。

这份笔记按提交逐节追加。

## 1. 预写日志：先写意图、落盘，再删

新模块 `media_agent/disposal.py`，所有硬删除的唯一出口 `hard_delete(log, path, size, **facts)`：

1. `lstat`：只删普通文件（目录、符号链接一律不删、连意图都不写）；
2. 往 `state/purge.jsonl` 追加 `{"op": "purge", "phase": "intent", "id": "<批次>#<n>", "path",
   "bytes", "disposition", "reason", …}`，`flush` + `fsync`；
3. `unlink`；
4. 追加 `{"phase": "done"}`，unlink 失败则 `{"phase": "failed", "error"}`。

**中断的两种形态都能认出来**（`disposal.recover`，每次处置开头跑）：意图悬着而文件还在 → 记
`abandoned`，文件照常重新评估；文件已不在 → 补 `done`（`recovered: true`）。恢复只补记录，
不替上一轮删任何东西。意图写不进去（盘满）→ 不删：没有记录的硬删除一次都不许发生。

记录与会话里手工处置的（`hard_delete` / `version_swap` …，形如 `{"op", "from", "to"}`）同在一份
`purge.jsonl`；新记录不带 `to`，`purge._manual_by_trash_path` 不会把它们误认成手工移入。

**删空了的目录逐层 `rmdir`**（`sweep_empty_dirs`），非空的 rmdir 失败正好不动；**从不 `rmtree`**：
整目录删除会把同一日目录里此刻不该删的东西一起带走——时间清理就是这么删掉 `purge` 拒绝的那些的。

`purge --apply` 先换到这条路上（判定仍是旧的 `build_pool`，后续各节改）。

**测试**：`tests/test_purge_log.py`——unlink 那一刻盘上已有意图；死在意图与 unlink 之间 / unlink 与
done 之间，下一轮分别记 abandoned / 补 done；unlink 失败记 failed；日志写不进去就不删；目录与符号
链接不删；清空目录不调 `rmtree`；`purge --apply` 先意图后删。改前 `purge --apply` 那条红（先删后记）。
