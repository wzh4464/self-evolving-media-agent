# `file_only` 不复核种子文件数，留下一批修不好的孤儿种子

Status: implemented
Class: bug-fix
Rule: 执行器 `_op_trash`（影响 `extras-in-library` 等所有设 `file_only` 的检测器）

## 现象

连续几轮 `diagnose` 都报同样的 5–6 条，且**永远修不掉**：

```
【攻壳机动队】
  🟡 [stale-torrent-path] 种子路径失效且无法自动定位（0 个文件对不上）：
     攻壳机动队 THE GHOST IN THE SHELL S01E01.mp4
  …E02 / E03 / E04 / E05 同样
```

`relink-torrent` 靠体积匹配重新关联，而磁盘上根本没有对应体积的文件，
所以它诚实地报"0 个文件对不上"、不给动作——检测器没错，是上游留下的烂摊子。

## 根因

`ExtrasDetector` 产出 trash 动作时一律带 `file_only: True`
（`plugins/builtin.py`），注释写的是"种子内其余正片保留，仅该文件设为不下载"。

执行器据此走另一条分支：

```python
if h and self.ctx.qbit and not file_only:
    self.ctx.qbit.delete([h], delete_files=False)   # 整个种子作废
elif h and self.ctx.qbit and file_only:
    ...set_file_priority(h, [idx], 0)               # 只把该文件设为不下载
```

这个设计**对合集种子是对的**：一个种子含 12 集正片 + 1 个 NCOP，
你只想去掉 NCOP，当然不能把整个种子删了。

但检测器并不知道种子里到底有几个文件。攻壳这些种子**每个只含一个文件**，
于是发生的是：文件被移进隔离区 → 种子记录还赖在 qBittorrent 里 →
指向一个已经不存在的路径 → 每轮诊断都报一次，谁也修不了。

审计日志里看得很清楚（批次 20260818T060115，5 条全是 `file_only=True`）。

## 修复

在**执行器**里按种子的实际文件数复核，而不是逐个去改检测器——
一处修复覆盖所有现在和将来设 `file_only` 的规则：

```python
if h and self.ctx.qbit and file_only:
    wanted = [e for e in self.ctx.qbit.files(h) if e.get("priority", 1) != 0]
    if len(wanted) <= 1:
        file_only = False          # 只含这一个文件 → 退化成整种子作废
```

用"未被设为不下载的文件数"而不是文件总数：种子里可能已有先前被禁用的文件，
那些不该算进"其余正片"。

## 存量清理

6 个孤儿种子（攻壳机动队 E01–E06 的重复版本）经 qBittorrent
`delete(delete_files=False)` 移除，未动任何文件。

清理脚本带一道安全阀：**只删有替代品的**——同一集必须已被另一个"文件确实存在"
的种子覆盖才允许删。实测拦下了 1 个：E06 那条的 `content_path` 是未改名的原始
发布名（用 `[06]` 而非 `S01E06`），集号提取失败 → 判定无替代 → 保留。
人工核实确有替代后才单独处理。宁可漏删也不要悄悄抹掉唯一记录。

清理后全库失效路径 **6 → 0**。

## 教训

**"只删文件不删记录"这类半步操作，必须校验前提是否成立。**
`file_only` 的前提是"种子里还有别的东西值得留"，检测器在没有能力验证这个前提的
位置上直接断言了它。这类断言应该下沉到有信息的那一层——执行器手里有 qBit 连接，
一次 API 调用就能确认。
