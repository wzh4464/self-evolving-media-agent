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

## 4. 抓取后的即时改名走闸门；保留文件夹层（N15）

**以前**：`_rename_grabbed` → `rename_single_video` 加完种子就 `renameFile` 到
`{标题} SxxEyy.ext`，什么都不查，返回值被丢掉，异常吞进一行日志——结局到不了审计。
新种子刚拿到元数据，libtorrent 只改映射，于是：

- **S3 与 AutoBangumi 赛跑**（grab 调研 S3）：诊断之后、加种之前 AB 下完并改名到 X。新种子
  被映射到 X，完成时 `X.!qB → X` 撞 EEXIST，偏好的版本作为孤儿半成品静默留下。
- **S1 停滞放行换源**（grab 调研 S1）：抓取是 op 0、摘死种是 op 1。改名那一刻旧种子还声明
  着 X、盘上有它的 `X.!qB`，新种子接着往那份半成品里写。
- 同一形态就是 2026-09-06 尼古喵喵 S01E08 丢片的根因：两个种子声明同一路径。

另外 `want = stem + 扩展名` 丢了条目的文件夹层（N15）：409 撞上已有的 Original 布局种子时，
文件被挪到 save_path 根下。

**现在**：
- `rename_single_video(qbit, h, stem, files, *, claims=None) -> RenameOutcome`：目标
  `save_path/文件夹/stem.ext`（save_path 问 qBittorrent 要，409 时可能在别处），
  `claims.check(target, own_hash=h, own_path=当前路径)` 不空闲（被占或看不全）就不改；
  改了就 `claims.invalidate()`。不给 `claims` 就现建一个。
- `RenameOutcome.audit()` → 抓取 applied 记录的新字段 `rename`：
  `{"renamed": 新条目名|None, "skipped"?: 原因, "claims"?: ClaimCheck.audit()}`。
  被拦时另记一行 `[grab] 加种后不改名：…` 日志（不含"失败"字样；真出错时仍是
  `[grab] 加种后改名失败`）。
- 加种之后 `_claims().invalidate()`：多了一个种子。
- 被占时 `ma:` 钉子本来就在（加种时打的），没有任何东西摘它。收敛靠已有流程：新种子下完，
  duplicate-episode 凭钉子封存它、把占位的清进隔离区（op 5），同一轮 unrenamed-file 把它
  改到集位名（op 6，再问一次闸门，此时已空闲）。

**S1 还没收敛**（有意为之）：死种只摘记录，`X.!qB` 留在盘上；新种子下完也改不过去——闸门
按设计拦下，改过去就是往那份半成品里写。处置"种子已摘、没人认领的 `.!qB`"属于删除闸门。
`test_grab_stale_bypass.py` 里有一条 `xfail(strict=True)` 记着它，那边落地后应当转绿。

**基座**：FakeWeb 拿 Mikan 站点标题当单文件名，标题常带 ` / `；libtorrent 把路径元素里的
分隔符换成 `_`，FakeQbit 以前没做，出现了生产上不存在的"文件夹/文件"条目（前一个提交已对齐）。

**测试**：`tests/test_grab_claims.py`——0% 的他人声明、AB 的完整文件（盘上 + 种子）、孤儿
`.!qB` 都不改且钉子与记账照旧、日志写明占用者；邻居 `files()` 读不到也不改；空闲时照改、
审计 `{"renamed": 集位名}`；409 撞上 Original 布局保留文件夹；`rename_single_video` 自建
索引、只改大小写放行；**端到端 S3**：诊断后 AB 抢先改名 → 抓取不改名 → 新种子下完 →
`converge()` 第一轮判重封存新种子、AB 那份进隔离区、同轮改名，最后只有新种子声明集位名。
`tests/test_grab_stale_bypass.py` 的换源用例改为断言"不改到旧种子仍声明的名字上"，另加上面
那条 xfail。

## 5. relink 与它的逆操作走闸门（N6 的原始场景）

**以前**：`StaleTorrentPathDetector` 按"字节数唯一"在剧目录（目录没了就全库）里给失联种子
找文件，从不看那个文件是不是已经归另一个活种子；`relink_torrent` 随后 setLocation →
renameFile → recheck。两个种子从此声明同一个文件；大小相同而内容不同时 recheck 判分片缺失，
qBittorrent 重下、覆盖到别人的文件上。逆操作把种子映射回原来失联的路径，那个路径此后若被
别的种子声明，回退同样造出双重声明。

**现在**：`Executor._relink_conflicts(h, cur_sp, mapping, new_sp, disk_for_mapped=…)` 算出
这个种子每个条目的最终落点，逐个问占用：
- 映射里的条目落到 `(new_sp or cur_sp)/映射后的名字`。正向只问 qBittorrent（那个文件本来
  就该在盘上，它就是要关联的对象）；逆向盘上 + qBittorrent 都问（目标是原来失联的路径，
  盘上此后有了别的文件同样算）。
- 换目录时不在映射里的条目会被 setLocation 连带搬走，目的地一并查。
- 正向：任何冲突 → skipped，理由含占用者 hash，`claims` 列出每个查询；看不全 → failed
  「未做任何改动」。**都在 setLocation 之前**——以前 setLocation 先发、改名全失败时
  save_path 已经挪了。逆向：冲突 / 看不全 → 这一条回退跳过并写明原因。

**测试**：`tests/test_relink_claims.py`——关联到无主文件照常（recheck 回到 100%）、可回退；
同目录的目标文件归另一个种子、换目录后的目标文件归另一个种子都拒绝且快照不变、一个
qBittorrent 调用都没有；邻居 `files()` 读不到 failed；回退时原路径被新种子占了就跳过。

## 6. 回退里往媒体库写路径的三个逆操作

**逆改名**：以前只看 `back.exists()`。原名此后被一个 0% 的新种子映射了（盘上什么都没有）照改，
两个种子声明同一路径；自己当初只改了大小写时，APFS 上 `exists()` 为真（就是它自己），回退
永远跳过。现在 `check(back, own_hash=h, own_path=cur)`：别的种子声明着 → 跳过「还原目标仍被
另一个种子声明」；盘上有别的文件 → 「还原目标已存在」（原措辞）；看不全 → 跳过「无法确认」。
有 hash 而 qBit 不可用的拒绝提到查询之前。条目仍按第 2 节的完整相对路径认。

**重加种子**（`readd_torrent`：死种、撞车、幻影摘记录的逆操作）：以前什么都不查。现在：
- 摘的时候（`_op_drop_torrent`、`_drop_record`，都在 `delete` 之前）把这个种子声明着的
  绝对路径记进逆操作的新字段 `paths`（`_claimed_paths`；读不到记 `[]`，不拦摘除本身）。
  `_undo_problem` 核对 `paths` 是列表、每项都在媒体库之下。
- 回退时候选路径 = `paths` ∪ 正向动作的 `args.path`（撞车 / 幻影的那个路径）∪
  `save_path/显示名`（单文件种子的显示名就是它原本的文件名，磁力重加回来的正是这个名字；
  第 2 阶段之前的旧记录只有它可用）。一个候选都没有 → 拒绝盲目重加。
- 每个候选只问 qBittorrent（盘上的半成品多半是它自己留下的），`own_hash` 取 magnet 的
  btih（base32 转十六进制），**`exempt` = 正向记录的 `keep_hash`**：撞车受害者被摘时保留方
  就声明着同一个文件，那是原状，回退就是要把它暂停着加回来、等人看。别的种子占了 →
  跳过并提示 magnet 在审计记录里。
- 局限：多文件种子重加回来用的是 .torrent 里的原始文件名，改名前叫什么 qBittorrent 的 API
  查不到；`paths` 记的是摘除时（可能已改过名）的路径。宁可按它拒绝，也不盲加——重加回来的
  种子之后要改名时还会再过一次 `_op_rename` 的闸门。

**从隔离区搬回**（`restore_from_trash`）：盘上已有 → 「原位置已被占用」（原措辞）；此外问
一次占用：一个还没落盘的下载可能已映射到这个名字（搬回去后它完成时撞 EEXIST），只差大小写
的文件在 APFS 上也是它。看不全同样跳过。

**索引**：`rollback` 每执行一步逆操作就作废（失败的也可能改到一半）。

**测试**：`tests/test_rollback_claims.py`——逆改名：原名被新种子占了跳过、快照不变；只改
大小写的回退不被自己挡住；读不到种子列表跳过。重加：死种被摘后换源的新种子占了同名 → 跳过且
提示 magnet；旧记录没有 `paths` 时退回显示名同样拦住；无人占用照常加回；撞车受害者不被当初的
保留方挡住（暂停加回），第三个种子占了就跳过。搬回：0% 下载已映射、只差大小写的本地文件
（原生与模拟区分大小写两种语义）、`torrents()` / 邻居 `files()` 读不到——都跳过、隔离区文件
原地不动。`fs` fixture 挪进 `tests/conftest.py` 供两个文件共用。

## 7. 目录改名的目的地（正向与回退）

**以前**：正向只看 `new.exists()`、回退只看 `back.exists()`。目的地盘上还没有，不等于没人占：
按新标题建的订阅、手动加的种子，save_path 可能已经指到它下面（0%、没元数据的也算）；或者一个
save_path 在媒体根、根文件夹恰好就叫这个名字的 Original 布局种子。setLocation 把我们的种子搬
进去，两边的文件混在一个目录里，同名集位互相争。

**现在**：`claims.check_dir(目的地, movers=要搬的那些种子)`。
- 正向：`affected` 改从占用索引的种子列表取（同一份快照）；读不到 → failed「未做任何改动」；
  盘上已有（含只差大小写）→ skipped「目标目录已存在，需人工合并」（原措辞）；别的种子声明着
  → skipped，理由含占用者。都在第一个 setLocation 之前。
- 回退：先认"改名后才落进来的种子"（第 1 阶段第 8 节），再问还原目标：盘上已有 → 「还原
  目标目录已存在」（原措辞）；别的种子 → 跳过；读不全 → 跳过。

**不做的**：正向 `_merge_tree` 在 setLocation 之后按文件搬残留时不跳过"此刻仍被种子声明"的
源文件（回退与 repair 在第 1 阶段已经跳过）；qBittorrent 的 setLocation 是异步的，理论上会与
残留搬运竞速（executor 调研："plausible, unconfirmed"）。留作后续。

**测试**：`tests/test_show_dir_claims.py`——新目录盘上没有但有 0% 种子的 save_path 指到它下面、
媒体根上根文件夹就叫新名的 Original 种子都拒绝且一个 setLocation 都没发；只差大小写的已有目录
按已存在（两种文件系统语义）；种子列表读不到 failed、快照不变；只有自己的种子照常改名；
回退时旧目录已被新种子指着就跳过。

## 8. 目录级搬运的"谁在这个目录下有文件"并入原语

第 1 阶段给目录改名的回退与 `repair` 写了 `Executor._live_claims_under(root)`：列出此刻在 `root`
之下有文件的种子与它们声明的全部路径，`_merge_tree` 据此绕开，交给 qBittorrent 自己搬。它是
第二个"读占用"的实现：逐字比较路径、每次重新问 qBittorrent、不认本批次已摘的种子。

现在是 `ClaimIndex.claims_under(root) -> ({hash: 视图}, {fold(路径): 路径})`，判据不变（save_path /
content_path 在 root 下，或有条目落在 root 下；优先级 0 的条目也算；含 `.!qB` 形态），比较改按
`fold`，走同一份批次索引。`_merge_tree(skip=…)` 按 `fold(src)` 查。`repair` 每对搬过种子后作废索引；
回退每步之后本来就作废。

**为什么要折叠**：盘上的名字与 qBittorrent 的条目只差大小写时（APFS 上是同一个文件——比如有人在
Finder 里改过大小写），逐字比较认不出它归种子，回退的残留搬运会在 qBittorrent 异步搬完之前用
文件系统把它搬走（AGENTS.md 第 3 条）。

**测试**：`tests/test_show_dir_claims.py` 末条（只在大小写不敏感的卷上跑：生产与 macOS 开发机）——
改名后盘上只改大小写、回退时 qBit 异步搬运：文件留在原地等 qBit，没有被文件系统搬走（改前红）。
`tests/test_path_claims.py` 末两条直接测 `claims_under`：优先级 0、上层 save_path 的 Original 种子、
`.!qB`、只差大小写都在清单里；相关种子读不到抛 `ClaimsUnknown`。
第 1 阶段 `tests/test_show_dir_moves.py` 的回退 / repair 用例全部照旧通过。
