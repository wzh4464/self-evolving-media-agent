# 变更记录

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 版本与发布约定

- **每个 tag 都是可部署单元。** 生产机只部署 tag，不部署分支头；回滚 = 部署上一个 tag。
- **一个阶段一个分支**，`--no-ff` 合回 `main` 后打 tag。分支内每个提交单独可测、测试全绿。
- **0.x 阶段**：minor 位表示一个整改阶段落地，patch 位表示阶段内的修复。
- **运行时产物也要入库。** 生产机上生成、且影响行为的文件（演进规则、偏好）必须进版本库，
  生产行为要能从 git 完整复现。纯状态（`state/`、各番 sidecar、`.env`）不入库。
- 仓库公开：任何提交都不得包含 IP、密钥、token。

## [Unreleased]

### 新增
- `CHANGELOG.md` 与版本约定。

### 变更
- 生产机上运行时生成的 28 条演进规则原样纳入版本库（`.agents/rules/`），
  此前生产行为无法从 git 复现。

## [0.1.0] - 2026-09-26

整改前的生产基线。此前 40 个提交（2026-08-17 起）从未打过版本号；
2026-09-26 逐文件核对了生产机与 `2baa0c3` 的校验和，确认一致后补打此 tag。

基线包含：扫描 → 17 条内置检测器诊断 → 按动作顺序执行（隔离区代替删除、配额上限、
审计 + 一键回退）→ 演进器 → 隔离区安全清理；「谁先出要谁」的抓取模型与偏好打分；
probe 探测字幕轨/时长判重；按番指定版本（sidecar `require_any`）；择源结果封存集位。

已知问题（驱动后续整改）：删除安全护栏散落在各检测器、执行器只查配额；
诊断快照与批量执行之间的状态滞后；与 AutoBangumi 双头下载/改名；
静默失败无人察觉；测试仅 3 个脚本、无 CI；生产部署靠手工 rsync。

[Unreleased]: https://github.com/wzh4464/self-evolving-media-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wzh4464/self-evolving-media-agent/releases/tag/v0.1.0
