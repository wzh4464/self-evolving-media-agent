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

## 2. `_op_rename` 接上原语；条目按完整相对路径认

**以前**：盘上 `target.exists()` 加第 1 阶段的 `_claimants`（另一个种子的条目与目标逐字符
相等）。看不见的：别人的孤儿 `X.!qB`（种子已摘、半成品留下——死种处置就是这样）；只差
大小写 / 规范化的声明。反过来，自己的文件只改大小写时 APFS 上 `exists()` 为真，被误报成
「集位被占」、永远改不成。`_torrent_rel_path` 按**文件名**找条目，合集里不同子目录下同名的
文件（`a/E05.mkv`、`b/E05.mkv`）改名与逆改名都会改到第一个（第 1 阶段只修了 file_only 隔离）。

**现在**：`chk = self._claims().check(target, own_hash=h, own_path=path)`：
- `unknown` → **failed**「无法确认目标路径的占用情况，未做任何改动」（与 `_op_trash` 读不到
  文件列表同口径：没改东西，但这一轮确实读失败了）；
- 有占用者 → skipped「集位被占」，`claimants` 列出每一个（kind / path / hash / name /
  progress / state / category）；理由分三种：别的种子声明着（含 hash 前 8 位，沿用第 9 条的
  措辞）、盘上有别人的半成品、盘上有别的完整文件（原措辞）。
- 有 hash 而 qBit 不可用的纵深防御提到占用查询之前，理由不变。
- 条目用 `_entry_at(t, entries, path)`：`save_path + 条目名 == path`，逐字相等（scan 就是这样
  拼出 MediaFile 路径的，逆改名记下的路径也由它推出）。`_torrent_rel_path` 同口径；种子不在
  列表里直接当"已不在"，不再去问 `files()`。

**执行器的索引**：`Executor._claims()` 一批次一份，`ignore` 按引用传入 `_removed_torrents`。
`_audit` 写任何非 skipped 记录时作废它——applied 与 failed 都可能已经改了东西。

**测试**：`tests/test_rename_claims.py`——下载中的文件不改到孤儿 `.!qB` 上；拒绝只差大小写的
他人声明；自己的种子文件 / 本地文件只改大小写照改；`torrents()` 读失败、同目录邻居的
`files()` 读失败都 failed 且没有 renameFile；合集按完整路径改对条目、逆改名也改对；找不到
条目仍然报失败、不退化成 mv。`tests/test_harness.py` 的 failed 审计样例把超时从 `files()`
挪到 `torrents()`：那个 hash 本就不在种子列表里，如今直接认作"已不在"。

## 3. 删掉零调用方的 `grabber.add_and_name`

`add_and_name`（加种 → 等元数据 30 秒 → 改名 → 无条件改显示名）零调用方：本地两份克隆、
tests / tools / deploy、生产机上全部 `*.py` / `*.sh` 都查过（grab 调研 §2）。在用的是
`_op_grab_episode` → `_rename_grabbed`（等 10 秒、只在分季集号与库内不一致时改显示名）。
`grabber.py` 的模块文档却说它是"加种子的唯一入口"。两条路径并存，占用闸门只接一条
等于没接，所以删掉，并把文档改成实情：加种的唯一 HTTP 入口是 `QBitClient.add_torrent`
（调用方 `_op_grab_episode` 与回退的 `readd_torrent`），`grabber` 只放加种之后的两步。
