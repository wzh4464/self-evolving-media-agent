# 按 git tag 原地部署 + 跨进程运行锁 + 演进冻结

**日期**: 2026-09-26
**状态**: implemented / process
**触发**: 部署调研发现生产 `~/media-agent` 没有 `.git`，靠手工拷文件；全项目没有一把锁
**追认**: 本笔记随 13704f6 入库，**追溯记录**此前五个提交的决策：653aff2（EVOLVE_MODE）、
0b5a02e（运行锁与批次 ID 格式）、d504b3c（launchd 入口）、e216f6e（.gitignore）、
c2eca6f（deploy.sh / convert-to-git.sh）。它们提交时各自都没带 Agent Note，违反了
AGENTS.md「写 Agent Note 的时机」；分支未推送但不改写历史，以此为准——单独检出、bisect
或 cherry-pick 其中任何一个时，决策记录在这里（下文「做了什么」第 1–5 条逐一对应）。

## 为什么需要

- **生产行为无法从 git 复现。** 生产目录的代码与 `2baa0c3` 一致，但 `AGENTS.md` 停在
  `1abcf1a`，4 个脚本和 14 篇笔记从没同步过，37 篇演进笔记与 28 条演进规则只在生产上
  （规则后来在 `bb51f98` 补录）。偏好文件也在生产上手改过一次（留下了
  `preferences.json.bak-20260905`）。"现在线上跑的是哪个版本"没有答案。
- **运行时联网装依赖。** launchd 执行的是 `uv run`，每轮按 `uv.lock` 同步环境；
  引入 pytest 之后锁文件变了，部署后的第一轮就要在凌晨联网装包（critic §3.7）。
- **没有任何互斥。** launchd 每 6 小时的 `run` 与手动 `apply` / `rollback` / `repair` /
  `purge --apply` 可以同时跑，各拿一份诊断快照各自执行；替换代码时也挡不住正在跑的一轮
  （critic N17、§3.6）。批次 ID 只精确到秒，同一秒的两批会被当成一个回退单元（critic N10）。
- **演进器往仓库里写文件。** 改成 git 部署后，这些未入库的文件要么被 checkout 覆盖，
  要么挡住部署（critic §3.8）。它已连续 147 轮提议 0 条。

## 做了什么

1. `EVOLVE_MODE=off|propose`，默认 `off`：`run` 不再演进、不调 LLM、不写 `.agents/`。
2. `media_agent/runlock.py`：`state/run.lock` 上的 `flock`。改动类子命令用
   `set_defaults(lock=...)` 声明要锁；拿不到最多等 10 秒，报出持有者、退出码 75（2026-09-27 起 `run` 等 300 秒
   `runlock.RUN_WAIT`：每 30 分钟的 `grab` 与它每 6 小时撞一次，见 `architecture/2026-09-27-grab-mode.md`）。
   批次 ID 改为 `YYYYMMDDTHHMMSS.mmm-<pid>`，旧 ID 照常可用。
3. plist 直接执行 `.venv/bin/media-agent run`，依赖只在部署时装。
4. `.gitignore` 挡住 `.env.*`、`*.bak*`、`.DS_Store`。
5. `deploy/convert-to-git.sh`（一次性、可撤销、原地）与 `deploy/deploy.sh <tag>`：
   漂移闸门 → 独立 worktree 暂存验证 → 持运行锁原地切换 → 失败自动退回 → 按需重装 plist →
   `state/deploy.history`。细节见 `deploy/README.md`。

## 取舍

- **原地 checkout，不做 `releases/<sha>` 软链。** 三处 `Path(__file__).resolve()` 会把软链
  解析成发布目录，`state/`、`.env`、规则、偏好随之分家；审计里存的是隔离区绝对路径。
  要做软链布局得先给这三处加环境变量覆盖——不值得。
- **偏好留在 git 里，不挪出仓库。** 它是影响行为的输入，版本化才能复现；代价是生产上
  改了必须提交，漂移闸门负责提醒（并能 `--harvest` 打包带走）。每部番的意图本来就在
  媒体库的 sidecar 里，不受部署影响。
- **锁用 flock、锁文件永不删除。** 进程死了内核自动释放，不存在陈旧锁；删文件会造成
  两把锁。shell 侧用 `lockf -k` / `flock(1)`，与 Python 侧互斥。
- **旧代码不认锁。** 转换后、第一次部署前，生产上跑的仍是 v0.1.0：`deploy.sh` 额外等
  launchd 报告那一轮结束。
- **没覆盖的**：`vpn-watchdog.sh`、`rescue.py` 重建 qBittorrent 容器时不看运行锁；
  回滚到 `v0.1.0` 会移除后来入库的 28 条（无动作的）演进规则。

## 验证

- 离线测试：`tests/test_evolve_mode.py`、`test_run_lock.py`（含真实第二进程、与
  `lockf(1)`/`flock(1)` 双向互斥）、`test_run_id.py`、`test_deploy_plist.py`、
  `test_gitignore.py`、`test_deploy_scripts.py`（替身 uv / launchctl，CI 的 macOS 腿用
  `/bin/bash` 3.2）、`test_version.py`。
- 按生产文件清单在临时目录复原生产目录，用真 uv 0.7.2 + 真 pytest 演练：转换（撤销后
  逐字节复原）→ 闸门拦下 37 篇笔记 → 入库打 tag → 部署 → 回滚到 v0.1.0 → 再部署 →
  bundle 部署；另测了锁超时、原地失败自动退回、偏好被改时的 diff 与 harvest、坏发布在
  暂存阶段被拦下。全程没有碰真生产。

## 审查后的补丁（2026-09-26）

### 部署被打断：不停在半路，重跑能补齐

**症状（审查在沙盒里复现，/bin/bash 3.2 + 真 lockf + 替身 uv / launchctl）**：切换阶段只装了
`trap ': > "$LOCK"' EXIT`。ssh 断线（SIGHUP）或 Ctrl-C 打在原地 `uv sync --frozen` / 离线测试上，
切换直接被杀、不调 `revert`、不写 `deploy.history`：HEAD 已是新 tag，venv 还是旧锁文件装的，
plist 没换，`ma-*` 临时文件泄漏（rc=129）。再跑 `deploy.sh <同一个 tag>`，`SHA == PREV` 的短路
报「已经是 v1.1.0，无需部署」并返回 0——venv 与 plist 永远补不上。下一次升级时新 plist 直接跑
`.venv/bin/media-agent`、不再同步依赖，某个 tag 一旦新增依赖，每一轮 launchd 都会失败，而运维者
被告知"部署完成"。

**修法**：
- 脚本开头 `trap '' HUP`：lockf、git、uv、pytest 一路继承，断线不再杀掉任何一步。
- 最外层把全部输出经 `tee -a state/deploy.log` 转一道：终端没了之后子进程写的是管道，不会因为
  EIO 失败；日志留下这次部署的结局。
- 切换阶段在第一次动生产目录之前装 `trap 'revert "被信号中断（INT/TERM）"' INT TERM`，`revert`
  自身先屏蔽 INT/TERM；`record ok` 之前撤掉这个 trap。
- 短路改为看 `deploy.history`：最近一次动过生产目录的记录（`ok / converted /
  converted-tests-failed / reverted / revert-failed`）是这个 commit 的 `ok` 或 `converted` 才算
  "已经是"；否则照常重走（HEAD 已在目标 commit 时 checkout 是空操作，sync / 测试 / plist 幂等）。

**测试**：`tests/test_deploy_scripts.py`——对整个进程组发 `kill -HUP 0`（`start_new_session`，
不波及 pytest）后部署照常完成、`deploy.log` 里有结局、临时文件清理；`kill -INT 0` 自动退回并记
「中断」；手工造出"HEAD 已是新 tag、其余是旧的"的现场后重跑同一 tag 会补齐；转换后与成功部署后
重跑仍是空操作。沙盒 fixture 补上了 `convert-to-git.sh` 会写的那行 `converted`。

### 目标 tag 里没有 deploy.sh 时，文档与回滚提示仍然可照做

**症状（审查在沙盒里复现，真 uv 0.7.2 + 真 pytest）**：转换停在 `v0.1.0`，它不跟踪
`deploy/deploy.sh`；README 的「转换之后、第一次部署之前」与「日常部署」却让人
`cd ~/media-agent && deploy/deploy.sh v0.2.0`。回滚同理：部署 `v0.1.0` 会把脚本从工作区删掉，
成功信息却打印 `回滚：deploy/deploy.sh v0.2.0`——恢复的那一刻照做得到 No such file or directory。

**修法**：成功信息的回滚提示由 `rollback_hint` 生成——刚部署的 tag（HEAD）里有脚本就照旧；
没有就从部署前的 tag（它有的话）`git show <tag>:deploy/deploy.sh > $TMPDIR/media-agent-deploy.sh`
再 `/bin/bash` 跑。README 写明第一次部署与"工作区停在 v0.1.0"时都用这个形式。

**测试**：沙盒上游 `v1.1.0` 起带 `deploy/deploy.sh`（`v1.0.0` 像 `v0.1.0` 一样没有）；部署 v1.1.0
的提示是 `deploy/deploy.sh v1.0.0`；再回到 v1.0.0 后，把打印出来的那条命令原样执行，能部署回 v1.1.0。
