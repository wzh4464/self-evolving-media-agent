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
- 离线测试基座（`uv run pytest`）：按生产 qBittorrent v5.2.3 实测语义建模的
  FakeQbit，以及 FakeWeb / FakeProbe / FakeTMDB / FakeAB / FakeLLM、
  跑在临时 sqlite 上的真 `AutoBangumiDB`、声明式搭库的 `LibraryBuilder`。
  测试全程断网、不起子进程、`state/` 落在临时目录、不读 `.env`；
  tripwire 让被吞掉的失败（failed 审计、检测器异常、未配路由的请求）
  直接判测试失败，防止假对象不完整时测试空转变绿。pytest 作为 dev 依赖；
  新锁文件已核实生产的 uv 0.7.2 可 `sync --frozen`。
- 五个端到端冒烟测试，离线跑通真实流程：发布名文件经 renameFile 改名并收敛、
  判重把输家移进隔离区且赢家同批拿到集位名、一键回退让磁盘复原、
  抓取加种（`ma:` 钉子 + 剧名分类 + 同批记账不被覆盖）、订阅别名修复走真 AB 库。
- GitHub Actions（`.github/workflows/tests.yml`）：push 到 `main` / `phase/**` 与
  PR 时跑离线测试，矩阵为 Ubuntu × Python 3.12（生产解释器，装 ffmpeg 跑探测一致性）
  / 3.14，以及 macOS × 3.12（生产媒体卷是大小写不敏感的 APFS）；另有一个任务用
  生产的 uv 0.7.2 核对锁文件可读、不带 dev 组可装、CLI 能起来。

### 变更
- 生产机上运行时生成的 28 条演进规则原样纳入版本库（`.agents/rules/`），
  此前生产行为无法从 git 复现。
- 三个脚本式回归测试（`test_seal_slot` / `test_grab_bookkeeping` /
  `test_no_phantom_duplicate`）迁移为 pytest，每条原有检查都保留。
  抓取记账原先靠切 `actions.py` 源码文本检查，文件一拆就会崩，
  改为真跑一次抓取并核对 `applied` 审计与 `ungrab_episode` 逆操作；
  "两个种子宣称同一路径"改为离线搭现场验证；对生产全库的只读检查
  移到 `tests/live/`，默认不跑（`MEDIA_AGENT_LIVE=1 uv run pytest -m live`），
  且媒体卷不在或 qBittorrent 登录失败时明确失败，不再空转通过。

### 修复
- **回退可能清空当前目录**（critic N1 / LAT-02）：审计里 `trash_path` 为空的记录
  （生产上 6 条）回退时会把运维者的当前目录拷进媒体库再整个删掉。现在每个逆操作
  先核对参数（非空、绝对、规范化、在隔离区 / 媒体库之下、文件名是单个分量、
  hash 与 magnet 非空），不合法就跳过并写明原因；`repair` 同样过这道闸。

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
