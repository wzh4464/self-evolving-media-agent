# 幻影重复：两个种子宣称同一路径，判重删掉了唯一的真文件

**日期**: 2026-09-06（发生）/ 2026-09-07（定位并修复）
**状态**: implemented / bug-fix
**代价**: 尼古喵喵 S01E08 原件丢失（已从隔离区恢复），其种子被孤儿规则一并清除

## 症状

审计里出现了一行自相矛盾的记录：

    2026-09-06T01:40:48  duplicate-episode  trash  applied
        S01E08 重复：保留 尼古喵喵 S01E08.mkv，清理 尼古喵喵 S01E08.mkv

**keeper 和 loser 是同一个文件名。** 执行后集位变空，库里没有 S01E08 了。
随后 `orphan-torrent` 看到种子失去文件，把种子也删了。

## 成因链

前一天做过一次换版：E08 从 `[Dynamis One]` ABEMA 生肉换成 `[LoliHouse]` 邪竜解放版。
操作是「停掉旧种子 → 把旧文件移进隔离区 → 加新种子 → renameFile 成集位名」。

问题出在**旧种子留在 qBittorrent 里没动**。它的 `content_path` 仍然是
`/Media/尼古喵喵/Season 1/尼古喵喵 S01E08.mkv`，而新种子 renameFile 之后
落到的正是同一个路径。于是：

    scan.py 来源 1：逐个种子遍历 torrents/files，为每个 entry 发一条 MediaFile
      → 旧种子发一条（路径 X，声称 745MB）
      → 新种子发一条（路径 X，声称 566MB）
      → 磁盘上其实只有一个文件

    duplicate-episode：桶里 2 条 → 排序 → 清理 loser
      → loser.path == keeper.path == X → 把唯一的真文件移进了隔离区

## 为什么已有的安全网没接住

`StaleTorrentPathDetector` 正是为「陈旧 content_path」设计的，但它的判据是：

    if not cp.startswith(root) or Path(cp).exists():
        continue        # 路径存在 → 认为健康

它只在 content_path **指向的文件不存在**时才报警。这次旧种子的路径恰好被
**另一个种子的文件**占住了，看起来完全健康。
**盲区：陈旧路径撞上被复用的文件名。**

## 修复（两层）

1. **scan.py — 一个路径只产出一条 MediaFile。**
   多个种子宣称同一路径时，按 `(磁盘大小 == 种子声明大小, 进度, 文件存在)`
   打分选出真正的拥有者，只发这一条。这是治本：路径是文件的身份，
   同一路径出现两条本身就是错的。

2. **builtin.py `DuplicateEpisodeDetector` — 产出删除动作前按路径去重。**
   兜底。删除不可逆，不能指望上游永远不出错。

## 教训

**换版本时，旧种子必须一起处理**——只移文件不动种子，会在 qBittorrent 里
留下一个继续宣称该路径的幽灵。正确做法是先把旧种子 `setLocation` 到别处
（比如 `Media/.staging/retired`，仍在容器挂载内，种子可继续做种），
再让新文件进入集位。

顺带记一个环境事实：qBittorrent 跑在 `qbit-vpn` 容器里，**只挂了
`/Volumes/Backup/webdav/Media` 一个目录**。保存路径设在这棵树之外
（比如 `/Volumes/Backup/webdav/.staging`）会让种子连上源之后立刻转 `error`，
表现得像"没有做种者"，很容易误判成资源问题。

## 回归测试

`tests/test_no_phantom_duplicate.py`：
1. 对**真实库**跑一遍 `build_state`，断言没有两条 MediaFile 共用同一路径；
2. 构造两条同路径条目喂给 `DuplicateEpisodeDetector`，断言不产出删除动作。

已验证这个测试不是空过：把 `builtin.py` 换回修复前的备份，第 2 项立刻复现
出一模一样的 `保留 X，清理 X`，删除目标就是那个真实路径。
