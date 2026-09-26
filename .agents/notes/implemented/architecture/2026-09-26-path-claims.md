# 路径占用：所有"往库里落一个名字"的动作共用一道闸

**日期**: 2026-09-26
**状态**: implemented / architecture
**触发**: critic N6（"这个路径是否已被另一个活种子声明"应当是所有写路径共用的一道闸）、
N15（抓取后即时改名丢掉文件夹层）、grab 调研 §5 的闸门规格；第 1 阶段只给 `_op_rename`
接了一个按完整字符串比较的 `_claimants`（`bug-fix/2026-09-26-destructive-path-hotfixes.md`
第 9 节的"遗留"）。

这份笔记按提交逐节追加。

## 1. 原语：`media_agent/claims.py`

**为什么盘上看不到不等于没人占**（qBittorrent 5.2.3 / libtorrent 2.0.13）：

- `torrents/renameFile` 的 409 只管同一个种子内部的重名，跨种子不查。
- libtorrent 只在**源文件存在**且目标存在时报 "File exists"。源文件还没落盘（0%、刚拿到
  元数据）时只改映射；下载中的文件在盘上叫 `X.!qB`（生产 `incomplete_files_ext=True`），
  改名是 `A.!qB → X.!qB`，从不和完整的 `X` 比。两个种子于是无声地宣称同一路径，
  直到完成时 `X.!qB → X` 撞上 EEXIST。
- 生产 2026-08-29 穹庐下的魔女 S01E09：`- 09` 下载中被改到 X，`- 09v2` 完成后也被改到 X
  （盘上没有 X，照改），留下 789MB 的孤儿 `X.!qB`；2026-09-06 尼古喵喵 S01E08：两个种子
  声明同一路径，判重把唯一的真文件当输家清走。

**判据**——任何一个命中就算占用：

1. 盘上 `X` 或 `X.!qB` 存在；问的人自己的文件（`own_path`、`own_path.!qB`，按 inode）不算。
2. 另一个种子有优先级非 0 的条目，`save_path/条目名` 等于 `X` 或 `X.!qB`——0%、刚拿到
   元数据的也算；问的人自己的那个条目（`own_hash` + `own_path`）不算。

**比较口径 NFC + casefold**（`claims.fold`）。生产媒体卷是大小写 / 规范化都不敏感的 APFS。
盘上那一侧先直接 `lstat`（APFS 上本来就不敏感），再列父目录按折叠后的名字比——CI 的
Linux 区分大小写，只能靠后者。只改大小写的自我改名自然放行：撞上的正是自己的 inode /
自己的条目；同一路径若还有**别的**种子声明，照样算占用。

**看不全 = 不知道 = 拒绝**：qBittorrent 不可用、`torrents()` 读失败、相关种子 `files()`
读失败、目录读不了 → `ClaimCheck.unknown` 非空。`files()` 404（列出之后被删）不算看不全。

**索引**：`ClaimIndex` 一批次一份，按需建——`torrents()` 一次，`files()` 只问 save_path
是目标祖先的种子（生产一个 Season 目录约 40 个，全库 539 个），问过的缓存住；调用方每做完
一次改动就 `invalidate()`。批次外的并发改动（AutoBangumi 60 秒一次的改名）缓存与否都挡不住，
那是运行锁与分类所有权的事。

**目录**：`check_dir(target_dir, movers=…)`——盘上已有这个名字（不豁免只差大小写的自己：
大小写不敏感的卷上 setLocation 改不动目录名的大小写，与以前 `new.exists()` 在 APFS 上的行为
一致），或 `movers` 之外有种子的 save_path 在它之下（没有元数据的也算）、或有条目落在它之下。

**不做的**：metaDL（还没有元数据）的种子不知道文件名，文件级查询看不到它；目录级查询按
save_path 看得到。`download_path`（生产 `temp_path_enabled=False`）不看：条目完成后落在
`save_path/名字`，占用按那里算。

**测试**：`tests/test_path_claims.py`——两个种子争一个名字、另一个种子的 `X.!qB`、种子已摘
而孤儿 `.!qB` 仍在、0% 已映射、Original 布局按完整路径、优先级 0 与本批次已摘的不算、
大小写与 NFC/NFD、自己只改大小写放行而第二个声明者照样拦、只问 qBittorrent、qBit 不可用 /
`torrents()` 失败 / 相关种子 `files()` 失败都是 unknown、无关种子读失败不拦、404 不算看不全、
缓存与作废、目录查询。盘上相关的用例在"本机原生"与"模拟区分大小写的 lstat"两种语义下各跑
一遍（本机临时目录是 APFS，不模拟的话折叠列目录那一支在本机永远走不到；变异验证：
关掉列目录或 `fold` 都会变红）。
