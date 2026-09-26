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

## 2. `run` 不再按日期 `rmtree`，按处置类别逐个处置

`Executor.purge_trash` 删掉。`run` 末尾改调 `disposal.dispose(ctx, mode="run")`；`purge` 是同一个
处置器的手动入口（默认预演，`--apply` 用 `mode="manual"`）。**一套判据、两种时机**：

| 处置类别 | 判据（`purge.build_pool`） | `run` | `purge --apply` |
|---|---|---|---|
| `extras` | 过了 `TRASH_RETENTION_DAYS` | 删 | 删 |
| `dead_partial` | 过了保留期，且是 `.!qB` | 删 | 删 |
| `duplicate` | 证明得了替代者（第 3 节起逐条加严） | 过了保留期才删 | 证明得了就删 |
| `bundled_version` / `manual` / `other` / 没有记录 | —— | 不删，过了保留期逐个报 | 不删 |

- **处置类别从哪来**：新记录用删除关口记下的 `deletion.disposition`；旧记录（9,619 行没有这个字段）
  按规则 / kind 推断，与关口共用一张表（`gate.disposition_for`，从 `disposition_of` 拆出来）——
  `duplicate-episode` 的 `bundled_version` 仍是 `bundled_version`，不会被当成普通判重。
  **没有审计记录的一律 `other`**：以前 `build_pool` 还会凭 `purge.jsonl` 的手工记录、或者路径 + TMDB
  反推集位去证明它们可删；可那样"是哪一集"只来自文件名，没有任何检测器按规则判过它是什么。
  手工记录只用来说明它从哪来（`origin`）。`.!qB` 与目录元数据（`.nfo` / `.media-agent.json`）的
  无记录快车道随之取消——它们今天在生产隔离区里一个都没有（测绘：27 个文件全部有审计记录或
  version_swap 记录）。
- **特典到期即删是用户的明确口径**（2026-09-04 核对 extras 与 TMDB 的结论：「留在隔离区，按
  TRASH_RETENTION_DAYS=30 到期清除」）。时间清理删掉之后得有东西接住它，不然特典只进不出。
- **死种只认 `.!qB`**：第 1 阶段之前死种处置搬的是 `content_path`，NoSubfolder 多文件种子的
  `content_path` 就是整个 Season 目录（testinfra B1）；那种记录里的完整正片交给人。
- **保留期内的判重 `run` 不删**：回退（`restore_from_trash`）要用它们，生产上从隔离区捞回来的最晚
  隔了 19 天（义妹生活 S01E01-10，purge.jsonl）；`purge --apply` 是人要的，可以提前放。
- **过了保留期没删的逐个报出来**（`run.log` 里「隔离区：N 个文件已过保留期 30 天、没有自动删」），
  理由各自写明。以前它们会被 rmtree 掉——尼古喵喵 TV 版、6 个 NUKITASHI 字幕在 2026-10-14 之后。
- 处置在降级的一轮里不跑（qBittorrent 不可用整轮拒绝，与以前的时间清理一样）；`dispose` 自己也在
  非预演、`qbit=None` 时拒绝。预演只读：不补中断记录、不 rmdir。

**测试**：`tests/test_quarantine_disposal.py`——过了保留期的日目录里，特典逐个删并记录，合并发布的
另一版本 / 手动 / 没有记录的留着且在输出里逐个点名；保留期内什么都不动；预演不写不删；旧记录按规则推断
六种类别，新记录用关口记的；死种记录里的完整文件不删；真实一轮隔离的特典 30 天后删；证明得了的判重
`run` 等满保留期、`purge --apply` 提前放；证明不了的过了保留期留着并报出来。改前全红（`run` 那条红在
`rmtree`）。`tests/test_purge_log.py` 的 `purge --apply` 用例从"无记录的 `.!qB`"换成过期特典。
