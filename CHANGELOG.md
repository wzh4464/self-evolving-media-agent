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

## [0.2.0] - 2026-09-26

**安全与可复现。** 整改第 1 阶段：先把一条命令之遥的数据灾难堵上，再让生产行为
可以从 git 完整复现、按 tag 部署与回滚。不改变任何抓取、改名、判重的业务口径。

- 回退不会再因为空的 `trash_path` 把当前目录整个复制进媒体库再删掉（生产上 6 条记录受影响）；
  隔离永远只处置单个文件，死种不再能搬走整个 Season 目录；qBittorrent 不可用或读不全时整轮拒绝改动。
- 离线测试基座与 CI：测试从 3 个脚本增至 292 个，每个提交单独全绿。
- 自演进默认冻结；跨进程运行锁；launchd 不再在运行时联网同步依赖；按 tag 部署的 `deploy.sh`。

条目里的 `critic N…` / `critic §…`、`testinfra B…`、`LAT-…`、`runloop §…` 是整改前那轮只读测绘的
编号，逐条的说明、日期与批次 ID 见
[审计编号索引](.agents/notes/implemented/process/2026-09-26-phase1-audit-index.md)。

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
  生产的 uv 0.7.2 核对锁文件可读、不带 dev 组可装、CLI 能起来。引用的 action 钉在
  提交 SHA 上（最初写的 `astral-sh/setup-uv@v10` 在上游不存在，所有任务都起不来），
  由测试守住形状。
- **跨进程运行锁**（`state/run.lock`，`media_agent/runlock.py`）：`run`、`apply`、
  `rollback`、`repair`、`evolve`、`purge --apply` 同一时刻只能有一个在跑（含 `--dry-run`；
  `scan` / `diagnose` / `runs` / 只预演的 `purge` 不拿锁）。拿不到锁最多等 10 秒，
  然后打印持有者（pid、命令、开始时间）并以退出码 75（EX_TEMPFAIL）结束，什么都不做。
  锁是 `flock(2)`，进程被 kill 也会自动释放；与 `deploy.sh` 用的 `/usr/bin/lockf -k`
  互斥。此前没有任何锁：launchd 的一轮与手动命令、部署切换代码都可能同时进行。
- **按 git tag 部署**（`deploy/deploy.sh <tag>`）：只接受已存在的发布 tag（分支、commit
  一律拒绝）→ `git fetch --tags`（GitHub 不通可用 `--bundle`）→ **漂移闸门**：生产目录里
  改过的已跟踪文件（打印 diff）或 tag 里没有 / 内容不同的未入库文件一律拒绝部署，
  `--harvest` 把它们打包到 `state/harvest/` 便于带回开发机提交（`.env*`、`*.bak*` 永不入包）；
  与 tag 内容完全相同的未入库文件交给 tag 接管 → 在独立 git worktree + 独立 venv 里
  `uv sync --frozen`、`uv lock --check`、跑离线测试 → 持运行锁原地 `checkout --detach`、
  `uv sync --frozen`、再跑一遍离线测试，任一步失败自动退回部署前的版本 → plist 变了才
  `launchctl bootout/bootstrap` 重装（装不上连代码一起退回）→ 每次尝试记一行
  `state/deploy.history`。`--check` 只验证不切换。回滚 = 部署上一个 tag。
  ssh 断线（SIGHUP）不会把切换腰斩在半路（全程忽略 HUP，输出同时追加到 `state/deploy.log`）；
  切换中途 Ctrl-C / SIGTERM 自动退回并记「被信号中断」；HEAD 已是目标 tag 但 `deploy.history`
  没有它部署成功的记录时（旧脚本被打断留下的现场）不再报"已经是"，而是照常重走一遍补齐。
  部署到还没有 `deploy/deploy.sh` 的 tag（如 `v0.1.0`）之后，成功信息里的回滚命令改为从带脚本的
  tag 里 `git show` 出来再跑；部署手册写明转换后第一次部署同样这么做。
  兼容生产的 bash 3.2 / BSD 工具 / uv 0.7.2；同时只能有一个部署在跑。
- `deploy/convert-to-git.sh <tag>`：一次性把手工 rsync 部署的生产目录**原地**转成 git
  工作区（项目根由 `Path(__file__).resolve()` 定位、审计里存绝对路径，所以不搬家、
  不用 releases 软链）。先打快照（不含 `state/` 与 `.venv`，600），克隆到临时目录、以
  生产目录为工作区比对目标 tag：代码 / 依赖 / 规则 / 偏好有任何差异即中止且不动生产；
  文档类差异由 tag 落地；生产独有的文件（演进笔记等）打包并标出哪些 git 里还没有；
  之后 `uv sync --frozen`、离线测试、记 `deploy.history`，并打印原样撤销的三条命令。
- `media-agent --version`：版本号取自源码树的 `pyproject.toml`（可编辑安装切了 tag 还没
  sync 时，已装元数据是旧的），`deploy.sh` 切换后用它确认。
- `deploy/README.md` 重写为部署手册：什么在 git 里 / 什么只在生产、用户意图放在哪
  （每部番的意图在媒体库 sidecar；`.agents/preferences.json` 版本化，生产上改了要提交，
  漂移闸门会拦）、一次性转换与撤销、日常部署每一步的理由、回滚（代码 vs 数据）、
  运行锁、launchd、bash 3.2 注意事项。配套 Agent Note
  `process/2026-09-26-tag-deploy-and-run-lock.md`。

### 变更
- 生产机上的 37 篇演进笔记原样纳入版本库（`.agents/notes/`），与 28 条演进规则配套；
  转换到 git 部署时它们与 tag 内容一致，直接由 tag 接管。
- **批次 ID 不再撞车**（critic N10）：由秒级 `20260926T131502` 改为
  `20260926T131502.123-<pid>`。以前同一秒里起的两个执行器共用一个 ID，
  回退其中一批会把另一批一起撤掉（launchd 上 media-agent 与 vpn-watchdog 的
  周期同为 6 小时）。前缀仍是定宽时间戳、字典序即时间序；旧 ID 照常可列、可回退。
- **launchd 不再经 `uv run` 启动**（critic §3.7）：`deploy/com.zihan.media-agent.plist`
  改为直接执行 `~/media-agent/.venv/bin/media-agent run`。`uv run` 每轮都按 uv.lock
  同步环境，锁文件一变（引入 pytest 那次就变了）凌晨那轮就得联网装依赖，PyPI 不通
  就起不来；现在依赖只在部署时装。周期 21600 秒、Nice 10、LowPriorityIO、日志路径不变。
  需要重新 `launchctl bootstrap` 才生效（`deploy.sh` 发现 plist 变了会自动重装）。
- `.gitignore` 补齐生产机独有的东西：`.env.*`（生产上的 `.env.bak-20260917T205051`
  装着真密钥；`.env.example` 除外）、`*.bak` / `*.bak-*` / `*.bak.*`（生产上有 5 个
  `media_agent/*.py.bak-*` 与 `preferences.json.bak-20260905`）、`.DS_Store`（生产机
  没有全局 excludesfile）、工具缓存。生产目录改成 git 工作区后，`git add -A`
  不会再把它们带进公开仓库；`.agents/rules/` 与 `.agents/preferences.json`
  明确保持版本化，由测试守住。
- `media-agent run` 等命令在 qBittorrent 不可用或数据不完整时以退出码 3 结束
  （`cli.EXIT_DEGRADED`），launchd 的 last exit code 由此可见降级。
- 生产机上运行时生成的 28 条演进规则原样纳入版本库（`.agents/rules/`），
  此前生产行为无法从 git 复现。
- 三个脚本式回归测试（`test_seal_slot` / `test_grab_bookkeeping` /
  `test_no_phantom_duplicate`）迁移为 pytest，每条原有检查都保留。
  抓取记账原先靠切 `actions.py` 源码文本检查，文件一拆就会崩，
  改为真跑一次抓取并核对 `applied` 审计与 `ungrab_episode` 逆操作；
  "两个种子宣称同一路径"改为离线搭现场验证；对生产全库的只读检查
  移到 `tests/live/`，默认不跑（`MEDIA_AGENT_LIVE=1 uv run pytest -m live`），
  且媒体卷不在或 qBittorrent 登录失败时明确失败，不再空转通过。
- **自演进默认冻结**：新配置 `EVOLVE_MODE=off|propose`，默认 `off`——`run` 整段跳过
  演进（不重扫、不调 LLM、不往 `.agents/` 写规则或笔记），run.log 里记一行
  「演进：已冻结」；手动 `media-agent evolve` 在 `off` 下拒绝执行。`propose` 保留旧行为，
  `--no-evolve` 照旧可用。行为中立：2026-08-20 之后连续 147 轮提议 0 条，
  30 条演进规则全无动作。冻结是按 git tag 部署的前提——运行时往仓库目录写文件
  会让工作区与部署的 tag 不一致。`EVOLVE_MODE` 写错会以「配置错误」退出码 2 结束，
  不静默当成某个值。

### 修复
- **回退可能清空当前目录**（critic N1 / LAT-02）：审计里 `trash_path` 为空的记录
  （生产上 6 条）回退时会把运维者的当前目录拷进媒体库再整个删掉。现在每个逆操作
  先核对参数（非空、绝对、规范化、在隔离区 / 媒体库之下、文件名是单个分量、
  hash 与 magnet 非空），不合法就跳过并写明原因；`repair` 同样过这道闸。
- **隔离时先删种子、后查文件**（critic N1 根因）：文件不存在（种子声明了但盘上没有，
  或诊断后被挪走）时，以前种子记录照删、文件没搬，还留下空 `trash_path` 的逆操作。
  现在先确认目标是媒体库里真实存在的普通文件（目录一律拒绝），不存在就跳过、
  种子不动；删种子记录 / 设为不下载失败就停手，不再照搬文件；合集里只作废
  一个文件时按完整相对路径认条目；搬运失败时如实记下种子记录是否已删。
- **死种可能把整个季目录移进隔离区**（testinfra B1 / critic N7）：NoSubfolder
  多文件种子的 `content_path` 就是 `Season N` 目录。死种处置现在只摘它自己的
  种子记录（可凭 magnet 按原布局回退），磁盘一个字节不动；判死改用停滞时长
  （加入 / 最后收发数据 / 最后见到完整副本取最晚），不再用加入多久；只管媒体库
  番剧目录里的种子（不再碰 `.staging`）；有已下完的成员文件就只报告；执行前
  复核它仍然是死的；同一目录的多个死种不再被去重吞掉。
- **抓取会给"还活着"的旧种子另抓一份**：死种改按停滞时长判定之后，抓取的换源放行仍按
  加入时长——一个几天前加入、几小时前还在收数据的种子被放行换源，新种子改到同一个集位名上，
  旧种子却不算死、不被摘，两个种子抢一个文件。现在换源放行与死种处置用同一个判据，
  放行的那一轮旧种子一定同批被摘；有已下完成员的死合集不放行。
- **qBittorrent 登录失败或读不全时照样改动**（critic N2 / N3 / LAT-01）：2026-09-19
  一轮登录超时的运行把有种子的 `朱音落语 S01E12` 当成本地文件移进了隔离区。
  现在扫描把 qBit 不可用、`torrents()` / 任一种子 `files()` 读取失败都记下来
  （不再把失败缓存成空文件列表），此时 `run` / `apply` / `rollback` / `repair` /
  `purge --apply` / `evolve` 一律拒绝改动，打印 `⛔ 拒绝执行任何改动` 并以退出码 3
  结束（以前 `run` 永远返回 0）；降级的 `run` 也不演进、不做隔离区时间清理。
  有种子的文件无论正向还是回退都不再退化成文件系统改名。只读的 `scan` / `diagnose` 在 qBit 数据
  不完整时打印 `⚠️  qBittorrent 数据不完整（N 处）…——以下结论不可作为改动依据`，只预演的 `purge`
  在 qBit 不可用时打印 `⚠️  qBittorrent 不可用：缺种子证据，以下判定偏松，仅供参考`。
- **目录改名的回退与 `repair` 会用文件系统搬走活种子的文件**：回退时 setLocation 超时被吞掉、
  改名之后才落进新目录的种子（比如此后抓的新集）不在记录里，两种情况下文件都被整个搬回旧目录、
  种子失联，回退却记成已还原。现在先问 qBittorrent 此刻谁在目录里：有不在记录里的种子就整条跳过
  并写明是哪几个；setLocation 失败记为失败、残留一个不动；残留按文件逐个搬回并绕开所有种子声明的
  路径。`repair` 同样处理，失败的那一对打 ❌、命令以 1 退出；`repair --dry-run` 不再真的搬种子。
- **幻影会让两个种子宣称同一个集位，甚至赢下判重**：种子说已下完、盘上却没有的文件（LAT-01
  那次误隔离后留下的，或经 Jellyfin 删掉、手工挪走的）作为判重输家时，上一条修复让它的
  隔离变成"跳过"，赢家随即被改名到它仍在声明的名字上；它的声明大小或发布名更好时则反过来
  赢下判重，唯一的真文件被移进隔离区。现在幻影不参与封存、排序永远在真文件之后；幻影输家只摘
  种子记录（可凭 magnet 回退；合集里的一个条目只设为不下载，可恢复），不写隔离区逆操作；
  改名前还核对目标路径有没有被别的种子声明，有就跳过并写明是哪个种子。
- **还在下载的特典要等下完才处理**：上面"隔离前先确认文件存在"的修复把合集里下载中的
  NCOP / PV 也当成"文件不在"跳过，它们照下不误、下完下一轮才进隔离区，停滞的合集每轮写一条
  误导的跳过记录。现在下载中的特典立刻设为不下载（可恢复）；种子只剩它一个要下的文件时
  摘掉种子记录（可凭 magnet 回退），与修复前一致。
- **qBittorrent 报 0 个种子时当成"真的没有"**：登录成功、`torrents()` 返回空列表（比如容器
  重建时没挂上会话数据），每个有种子的文件都会被当成本地文件——改名走文件系统、隔离跳过种子。
  现在库里有视频文件而 qBit 报 0 个种子时，本轮按数据不完整整轮拒绝改动（退出码 3）。
  库里确实一个种子都不用的，设新配置 `QBIT_ALLOW_EMPTY=1`。
- **同批次里对已作废的输家再改名记成失败**（testinfra B2 / LAT-05，生产 5 次）：
  判重整种子作废输家后，它的改名去问已删种子而 404。现在执行器记下本批次移除的
  种子与搬进隔离区的路径，后续改名记为跳过并写明原因，不再污染失败模式统计；
  纯本地输家被搬走后不再误报「集位被占」；种子说已下完、盘上却没有的幻影不改名。
- **同一进程里重扫看到改名前的旧文件名**（testinfra B3）：种子文件列表缓存与
  `season_offsets` 缓存从不失效，`run` 在修复后为演进器重扫时会看到幻影重复与
  「未改名」。现在每次扫描开头清空这两份缓存。
- **演进规则的动作不经人审就执行**（critic N5）：LLM 提议、上线后不再复核的 DSL
  规则可以带 `trash` / `retag` 等动作，参数由模型选。现在这类动作一律跳过
  （「演进规则未经人工确认」），标记由解释器写死、规则文件无法冒充内置规则。
  生产上 30 条演进规则均无动作，行为不变。

## [0.1.0] - 2026-09-26

整改前的生产基线。此前 40 个提交（2026-08-17 起）从未打过版本号；
2026-09-26 逐文件核对了生产机与 `2baa0c3` 的校验和，确认一致后补打此 tag。

基线包含：扫描 → 17 条内置检测器诊断 → 按动作顺序执行（隔离区代替删除、配额上限、
审计 + 一键回退）→ 演进器 → 隔离区安全清理；「谁先出要谁」的抓取模型与偏好打分；
probe 探测字幕轨/时长判重；按番指定版本（sidecar `require_any`）；择源结果封存集位。

已知问题（驱动后续整改）：删除安全护栏散落在各检测器、执行器只查配额；
诊断快照与批量执行之间的状态滞后；与 AutoBangumi 双头下载/改名；
静默失败无人察觉；测试仅 3 个脚本、无 CI；生产部署靠手工 rsync。

[Unreleased]: https://github.com/wzh4464/self-evolving-media-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wzh4464/self-evolving-media-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wzh4464/self-evolving-media-agent/releases/tag/v0.1.0
