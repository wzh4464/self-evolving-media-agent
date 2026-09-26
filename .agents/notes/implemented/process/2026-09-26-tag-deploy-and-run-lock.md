# 按 git tag 原地部署 + 跨进程运行锁 + 演进冻结

**日期**: 2026-09-26
**状态**: implemented / process
**触发**: 部署调研发现生产 `~/media-agent` 没有 `.git`，靠手工拷文件；全项目没有一把锁

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
   `set_defaults(lock=...)` 声明要锁；拿不到最多等 10 秒，报出持有者、退出码 75。
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
