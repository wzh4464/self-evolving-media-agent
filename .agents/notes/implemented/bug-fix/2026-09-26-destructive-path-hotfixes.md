# 删改路径的安全热修：回退、隔离、死种、qBittorrent 失联

**日期**: 2026-09-26
**状态**: implemented / bug-fix
**触发**: 整改前的全库只读测绘（critic N1–N3、N5、N7；testinfra B1–B3）。
每一条都先在离线基座上把现场搭出来、看它失败，再修。

这份笔记按修复逐条追加，每条对应一个提交。

## 1. 回退前校验逆操作参数（critic N1 / LAT-02）

**症状（复现于 scratchpad，未在生产触发）**：`restore_from_trash` 写的是
`Path(u.get("trash_path") or "")`。空串变成 `Path('.')`——真值、而且"存在"——
于是 `shutil.move('.', dst)`。`os.rename('.')` 报 EINVAL，shutil 退回
`copytree(cwd → 媒体库)` 再 `rmtree(cwd)`：当前目录被整个拷进一个叫
`… S01E12.mp4` 的目录，然后清空。异常被回退的逐条 `except` 吞掉，回退继续跑下一条。

**暴露面**：生产审计里 6 条 trash 记录的 `trashed_to` 是 null——
20260830T132317（extras）、20260908T022758（dead-torrent ×3）、
20260908T143348 与 20260920T170126（duplicate-episode）。手动 rollback 的 cwd
就是项目目录（`.venv`、`.env`、`state/` 里的审计与 14GB 隔离区）。

**同类路径**：`rename_show_dir` 的逆操作对空 `path` 会把 cwd 里的每一项搬进
`cwd/<旧名>`；`restore_sidecar` / `ungrab_episode` 对空 `show_dir` 读写 cwd 里的
sidecar；`repair` 按审计里的空 `path` 在 cwd 里 `_merge_tree`；`relink` /
`recategorize` / `remove_tags` 对空 hash 静默"成功"。

**修法**：`Executor._undo_problem` 在任何逆操作动手前核对输入——
路径必须非空、绝对、已规范化（不含 `.`/`..`）、落在允许的根之下且不是根本身
（隔离区文件在 `state/trash` 下，其余在媒体库下）；文件名必须是单个路径分量；
种子内相对路径不得越出根目录；hash、magnet、bangumi_id 不得为空。
不合法就跳过并写明原因，绝不"尽力而为"。`restore_from_trash` 另外拒绝把目录
整体搬回；逆改名拒绝改目录；`repair` 用同一道闸筛审计记录。

**测试**：`tests/test_rollback_guards.py`——合成一条 `trash_path=""` 的生产形态
记录，在一个装着 `.env` / `state/audit.jsonl` 的假 cwd 里回退，断言 cwd 与媒体库
一个字节都没动；并对每个逆操作逐一喂坏参数。
