# 部署

生产目录 `~/media-agent`（zihan_air）是一个**停在某个发布 tag 上的 git 工作区**
（detached HEAD）。部署 = `deploy/deploy.sh <tag>`，回滚 = 部署上一个 tag。
分支、commit 一律不部署——生产上跑的必须是有名字、不可移动、在 CHANGELOG 里查得到的版本。

**为什么是原地 checkout，而不是 `releases/<sha>` + `current` 软链**：`config.py`、
`preferences.py`、`evolution.py` 用 `Path(__file__).resolve()` 定位项目根，软链会被解析成
各个发布目录，`state/`、`.env`、规则、偏好随之"分家"；`state/audit.jsonl` 与
`purge.jsonl` 又按**绝对路径**记隔离区里的文件，换个路径字符串，已有的隔离记录全部对不上。
所以代码原地切换，验证放在一个独立的 git worktree 里做。

| 文件 | 作用 |
|---|---|
| `deploy.sh` | 按 tag 部署：漂移闸门 → 暂存验证 → 持运行锁原地切换 → 失败自动退回 |
| `convert-to-git.sh` | 一次性：把手工 rsync 部署的目录原地转成 git 工作区（可撤销） |
| `com.zihan.media-agent.plist` | launchd 定时任务，每 6 小时跑一轮 `run` |
| `vpn-watchdog.sh` | 每 6 小时检查 gluetun 健康，不健康就重建容器 |
| `rescue.py` | BT 救援模式：把 qBittorrent 临时切到自有 VPS 的 WireGuard 隧道 |

## 什么在 git 里，什么不在

| 路径 | 在哪 | 说明 |
|---|---|---|
| `media_agent/`、`tests/`、`pyproject.toml`、`uv.lock` | git（tag） | 代码与锁定的依赖 |
| `.agents/preferences.json` | git（tag） | 全局择源偏好，**版本化**的行为输入，见下节 |
| `.agents/rules/`、`.agents/notes/` | git（tag） | 演进规则与 Agent Notes |
| `.agents/acks.json` | git（tag） | 已确认、先不提醒的"卡住"问题（`media-agent ack` 写），与偏好同理是版本化的用户意图 |
| `.env` | 只在生产（600） | 凭据与开关；`.env.*`（含 `.env.bak-*`）被忽略 |
| `.venv/` | 只在生产 | 由部署用 `uv sync --frozen` 维护，运行时不再同步 |
| `state/` | 只在生产 | `audit.jsonl`（回退的依据）、`audit.fallback.jsonl`（审计写不进主文件时的转写，回退一起读）、`purge.jsonl`、`cache.sqlite3`、`trash/`（隔离区）、`run.log`、`run.lock`、`deploy.lock`、`deploy.history`、`deploy.log`、`backups/`、`harvest/` |
| 各番目录里的 `.media-agent.json` | 媒体根下 | 每部番的用户意图，不归部署管，见下节 |

## 用户意图放在哪

- **每部番的意图**——"只保留 X 版"（`require_any`）、季内集号换算（`season_offsets`）、
  备注、来源、`mikan_id`——在媒体根下各番目录的 `.media-agent.json` 里。它们是状态，
  不入库、部署从不碰；要进备份。
- **全局择源偏好** `.agents/preferences.json` 是**版本化**的：每轮运行都重新读，在生产上
  改了当轮就生效——但下一次部署时漂移闸门会拦下它、打印 diff、拒绝部署。要保留就用
  `--harvest` 打包带回开发机，提交、打 tag，再部署那个 tag；不要就
  `git -C ~/media-agent checkout -- .agents/preferences.json`。生产行为必须能从 git 完整复现
  （CHANGELOG「版本与发布约定」）。
- **卡住问题的确认** `.agents/acks.json` 同样是版本化的：生产上 `media-agent ack …` 改了当轮就生效，下一次部署
  漂移闸门会拦下它——`--harvest` 带回开发机提交，或在开发机上 `ack` 之后提交、打 tag 再部署。
- **演进**默认冻结（`.env` 里 `EVOLVE_MODE=off`）：`run` 不再往 `.agents/` 写东西。
  设成 `propose` 会写 `.agents/rules|notes`，同样要提交入库才能再部署。

## 一次性转换（rsync 部署 → git 工作区）

前提：目标 tag 与生产上正在跑的代码**逐字节一致**。2026-09-26 核实过：生产 = `2baa0c3`
= `v0.1.0`（`media_agent/` 21 个文件、`pyproject.toml`、`uv.lock`、偏好、2 条种子规则全部相同；
另外 28 条演进规则与后来入库的 `bb51f98` 相同）。转换不改变任何运行行为。

```sh
# 在生产机上。v0.1.0 里还没有这个脚本：从含它的 tag 取一份（或 scp 过去）
curl -fsSLo /tmp/convert-to-git.sh \
  https://raw.githubusercontent.com/wzh4464/self-evolving-media-agent/<含脚本的tag>/deploy/convert-to-git.sh
bash /tmp/convert-to-git.sh v0.1.0
```

它做的事：

1. launchd 那一轮正在跑就拒绝；打快照 `~/media-agent-pre-git-<时间>.tgz`
   （不含 `state/` 与 `.venv`，含 `.env`，所以是 600）。
2. `git clone --no-checkout` 到临时目录，**以生产目录为工作区** `read-tree` 目标 tag，逐文件比对。
   `media_agent/`、`pyproject.toml`、`uv.lock`、`.agents/rules/`、`.agents/preferences.json`
   有任何差异就中止——此时生产目录一个字节都没动。文档、`deploy/`、`tools/`、`tests/`、
   `.agents/notes/` 等白名单里的差异（生产上从没同步过的、或者是旧版本的）允许，稍后由 tag 落地。
3. tag 不跟踪、生产上却有的文件（37 篇演进笔记、28 条演进规则）原样留在工作区，打包到
   `state/harvest/convert-<时间>.tgz`，并逐个标出"上游默认分支已有同样内容"还是"git 里没有——需要提交"。
   `.env*`、`*.bak*`、`.DS_Store` 由 `.git/info/exclude` 挡住，永远不会进包。
4. 把 `.git` 移进生产目录，HEAD 指向 tag，落地白名单差异，`uv sync --frozen`，跑离线测试，
   在 `state/deploy.history` 记一行。

**撤销**（结尾会把带实际路径的命令打印出来）：

```sh
rm -rf ~/media-agent/.git
tar -xzf ~/media-agent-pre-git-<时间>.tgz -C ~
(cd ~/media-agent && xargs rm -f < state/harvest/convert-<时间>.created.txt)   # 转换时新落地的文件
```

转换之后、第一次部署之前：把 harvest 包带回开发机，**提交那 37 篇笔记**（`scp` 过来
`tar -xzf … -C <仓库>`，审阅后提交），打新 tag。否则 `deploy.sh` 的漂移闸门会拦下它们。
第一次部署还会把 launchd 换成直接执行 `.venv/bin/media-agent`（见下文），并让
`EVOLVE_MODE` 的默认值 `off` 生效。

**第一次部署用不了 `deploy/deploy.sh`**：转换停在 `v0.1.0`，而 `v0.1.0` 里还没有这个脚本。
先取回 tag，再从目标 tag 里把脚本取出来跑（它会先复制自己再执行，从哪里跑都一样）：

```sh
git -C ~/media-agent fetch --tags origin
git -C ~/media-agent show v0.2.0:deploy/deploy.sh > /tmp/media-agent-deploy.sh
/bin/bash /tmp/media-agent-deploy.sh v0.2.0
```

之后工作区停在带脚本的 tag 上，就可以直接用下面的 `deploy/deploy.sh`。

## 日常部署

```sh
cd ~/media-agent
deploy/deploy.sh v0.2.0 --check     # 可选：只跑闸门与暂存验证，不切换
deploy/deploy.sh v0.2.0
```

（工作区当前的 tag 里没有 `deploy/deploy.sh` 时——转换后第一次部署、或回滚到了 `v0.1.0`——
用上一节的 `git show <带脚本的 tag>:deploy/deploy.sh > /tmp/…` 形式。）

1. **只认 tag。** 形如 `v0.2.0` 且 `refs/tags/` 下真有；`git fetch --tags origin` 若发现
   上游挪过同名 tag，git 会拒绝覆盖——已部署过的版本名不许换内容。
2. **漂移闸门。** 生产目录必须与当前部署的 tag 一字不差：
   - 改过的已跟踪文件（比如手改的 `preferences.json`）→ 打印 diff、拒绝；
   - tag 里没有、或同名但内容不同的未入库文件（比如演进器写的规则）→ 拒绝；
   - 与目标 tag 内容完全相同的未入库文件 → 交给 tag 接管（先挪开、切换失败时放回）。

   `--harvest` 把拦下的文件打包到 `state/harvest/<时间>-drift.tgz`，带回开发机提交。
3. **暂存验证。** 目标 tag 检出到一个独立的 git worktree，用它自己的 `.venv` 跑
   `uv sync --frozen`、`uv lock --check`、离线测试（`pytest`）。生产目录此时一个字节没动；
   失败就到此为止。
4. **切换（持运行锁）。** 等 `state/run.lock`（最多 `DEPLOY_LOCK_WAIT`=900 秒）；
   老代码（v0.1.0）不认锁，所以还要等 launchd 那一轮跑完。然后把小体量状态
   （`audit.jsonl`、`audit.fallback.jsonl`（有的话）、`purge.jsonl`、`cache.sqlite3`、已装的 plist）复制到
   `state/backups/<时间>-<部署前版本>/`，原地 `git checkout --detach <tag>`、
   `uv sync --frozen`、再跑一遍离线测试。任何一步失败都**自动退回**部署前的版本
   （含 venv 与被接管的文件），`deploy.history` 记 `reverted`。
5. **launchd。** tag 里的 plist 与已装的不同才重装（`plutil -lint` → `launchctl bootout` →
   `bootstrap gui/$(id -u)`）；装不上就把旧 plist 装回去，代码也一起退回。
   重装会让 `StartInterval` 从那一刻重新计时。
6. **记录与确认。** `state/deploy.history` 每次尝试一行：
   `时间 ⇥ tag ⇥ 目标 commit ⇥ 部署前 commit ⇥ 结果 ⇥ 说明`，结果是
   `ok / reverted / revert-failed / drift / stage-failed / lock-timeout / busy / checked / converted`。
   成功后打印 `media-agent --version`；它与 tag 对不上会警告（发版时忘了改 `pyproject.toml` 的版本号）。

同一时刻只能有一个 `deploy.sh` 在跑（`state/deploy.lock`）。脚本启动时先把自己复制一份再执行，
所以 checkout 改写 `deploy/deploy.sh` 本身不会影响正在跑的这一次。

**被打断了怎么办。** 建议在 `tmux` 里跑部署（`tmux new -s deploy`），断线后 `tmux attach` 回来看。
不用 tmux 也不会停在半路：

- **ssh 断线（SIGHUP）**：整个部署忽略 HUP，照常走完——成功记 `ok`，失败照常自动退回。全部输出
  同时追加到 `state/deploy.log`（经 `tee` 转一道，终端没了子进程也不会因为写不出去而失败），
  重新连上来 `tail -50 ~/media-agent/state/deploy.log` 看结局。
- **Ctrl-C / SIGTERM**：切换开始之前（闸门、暂存验证）直接退出，生产目录没动；切换开始之后
  **自动退回**部署前的版本，`deploy.history` 记 `reverted`、说明写「被信号中断」。
- **旧版本脚本被杀留下的现场**（HEAD 已是新 tag、venv 与 plist 还是旧的）：再跑一次同一个 tag
  即可。`HEAD == tag` 不再被当成"已经部署过"——只有 `deploy.history` 里最近一次动过生产目录的
  记录（`ok / converted / reverted / revert-failed`）正是这个 commit 的 `ok` 或 `converted` 时才短路，
  否则照常走一遍，依赖、测试、launchd 配置都重新落实（对完好的目录这一遍是幂等的）。

**GitHub 不通时**用 bundle 带 tag 过去：

```sh
# 开发机
git bundle create /tmp/ma.bundle --tags && scp /tmp/ma.bundle <生产机>:/tmp/
# 生产机
deploy/deploy.sh v0.2.0 --bundle /tmp/ma.bundle
```

**发版（开发机）**：阶段分支 `--no-ff` 合回 `main` → 改 `pyproject.toml` 的 `version` 与
CHANGELOG → 打带注释的 tag `vX.Y.Z` → `git push origin main vX.Y.Z` → 生产机上 `deploy.sh vX.Y.Z`。

## 回滚

- **代码**：`deploy/deploy.sh <上一个 tag>`（`state/deploy.history` 里查），走同样的闸门与验证。
  注意 `v0.1.0` 不跟踪那 28 条演进规则（它们后来才入库），回到 `v0.1.0` 会移除它们——
  它们全都没有动作、今天零命中，行为不变。
  **回到 `v0.1.0` 之后工作区里没有 `deploy/deploy.sh`**：成功信息里的"回滚"一行会改成打印
  `git -C ~/media-agent show v0.2.0:deploy/deploy.sh > <临时文件> && /bin/bash <临时文件> v0.2.0`
  这种形式——照着复制就能再部署回去（测试里照着打印出来的命令跑过）。
- **数据**：代码回滚从不回卷 `state/`。撤销某一批改动仍然是
  `media-agent rollback --run <批次 ID>`；因此每个版本都必须能读懂旧版本写下的 state
  （审计格式向后兼容是发版要求）。`state/backups/` 里的副本只供手工比对，不会自动还原。
- **自动退回也失败时**（`revert-failed`）：脚本会打印手工命令——
  `git checkout -f --detach <部署前 commit> && uv sync --frozen`，被挪开的文件在
  `state/backups/<…>/replaced-untracked.tgz`。

## 运行锁

`state/run.lock` 上的 `flock(2)`（`media_agent/runlock.py`）。同一时刻只允许一个会改动东西的进程：

| 拿锁 | 不拿锁 |
|---|---|
| `run`、`apply`、`rollback`、`repair`、`evolve`、`purge --apply`（含 `--dry-run`） | `scan`、`diagnose`、`runs`、只预演的 `purge` |

- 拿不到锁最多等 10 秒，然后打印持有者（pid、命令、开始时间）、以**退出码 75** 结束，
  什么都不做。launchd 的一轮撞上部署或手动操作就是这样：`last exit code = 75`，6 小时后再来。
- 进程被 kill 锁自动释放，不会留下要人去删的锁。**不要删 `run.lock` 文件**——
  删了之后持有者锁住的是一个没有名字的 inode，下一个进程会拿到另一把锁。
- `deploy.sh` 用 `/usr/bin/lockf -k`（Linux 上是 `flock(1)`）拿同一把锁，切换期间一直持有。
- 手工维护时想让 agent 暂停：`touch ~/media-agent/state/PAUSE`（里面可以写一句为什么），`run` / `apply` 就以 75 结束、健康报告 warn；维护完 `rm` 掉。VPN 救援期间（`~/gluetun/.rescue-active` 在）自动暂停。要在自己干活的那几分钟里连手动命令也挡住，就自己拿着锁：
  `/usr/bin/lockf -k ~/media-agent/state/run.lock zsh`（退出这个 shell 即释放）。
- `vpn-watchdog.sh` 与 `rescue.py` 重建 qBittorrent 容器之前同样拿这把锁（critic N17）：最多等 900 秒
  （`RUNLOCK_WAIT` / `RESCUE_LOCK_WAIT`），等不到就这次不重建、退出码 75。见下文「vpn-watchdog.sh / rescue.py」。

## launchd

`com.zihan.media-agent.plist` 直接执行 `~/media-agent/.venv/bin/media-agent run`，
**不经 `uv run`**：`uv run` 每轮都会按 `uv.lock` 同步环境，锁文件一变就在凌晨联网装依赖，
PyPI 不通这一轮就起不来。依赖只在部署时装。周期 21600 秒、`RunAtLoad false`、`Nice 10`、
`LowPriorityIO`，stdout / stderr 追加到 `state/run.log` / `state/run.err.log`。每一行带时间与批次 ID，每轮以「run 开始：批次 …」一行开头；两个文件超过 5 MB 时由 `run` 自己先拷贝再截断地轮转成 `.1` … `.5`（`media_agent/runlog.py`：launchd 持有描述符，不能改名；不需要 newsyslog）。
plist 由 `deploy.sh` 在变化时自动重装；手工重装：

```sh
launchctl bootout gui/$(id -u)/com.zihan.media-agent
cp ~/media-agent/deploy/com.zihan.media-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zihan.media-agent.plist
```

`launchctl list | grep media-agent` 的 last exit code（每轮的详情在 `state/health/<批次 ID>.json`，`media-agent health` 看最近一轮）：

| 退出码 | 含义 |
|---|---|
| 0 | 正常（健康报告 ok 或 warn；warn 的原因见 run.log 末尾的「健康」一节或 `media-agent health`） |
| 1 | 这一轮崩了（异常冲出）：完整 traceback 在 run.err.log，健康报告照写 |
| 3 | 降级、整批拒绝改动（qBittorrent 不可用、读不全，或种子数比上一轮骤降而审计解释不了——确认是人为删除的用 `media-agent health --accept-torrent-count`），什么都没改 |
| 4 | 改动照常做了，但有审计记录没能原样写进 `state/audit.jsonl`（磁盘满、权限……）——已转写到 run.err.log 与 `state/audit.fallback.jsonl`，`rollback` / `runs` 会一起读；先腾空间 |
| 5 | 这一轮跑完了，但健康报告 critical：隔离区处置之后媒体卷剩余仍低于 `MIN_FREE_GB`——要人腾空间 |
| 75 | 另一个进程持有运行锁，或维护暂停中（VPN 救援进行中 / 有 `state/PAUSE`），这一轮什么都没做 |

## 部署脚本自己的测试

`tests/test_deploy_scripts.py` 在临时目录里模拟生产目录（替身 uv / launchctl），覆盖闸门、
暂存失败、原地失败自动退回、plist 装不上、锁、转换与撤销；CI 的 macOS 腿用的正是生产机上的
`/bin/bash` 3.2。2026-09-26 另按生产文件清单复原了一个目录，用真 uv 0.7.2 + 真 pytest
走完了 转换 → 闸门拦下 37 篇笔记 → 入库打 tag → 部署 → 回滚到 v0.1.0 → 再部署。

改脚本时注意生产是 **macOS bash 3.2 + BSD 工具**：没有 `mapfile`、关联数组、`timeout`、
GNU 专有参数；空数组在 `set -u` 下展开会报错；**变量名后紧跟中文必须写成 `${VAR}`**——
bash 3.2 会把多字节字符当成变量名的一部分（`$APP，` → `APP\xef: unbound variable`），
测试会扫这一条。

## vpn-watchdog.sh / rescue.py 的配置从哪来

这两个脚本**不含任何基础设施地址**，全部从环境读：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEDIA_AGENT_HOME` | `~/media-agent` | media-agent 仓库位置（运行锁在它的 `state/run.lock`） |
| `DOCKER_BIN` | `/usr/local/bin/docker` | docker 可执行文件（`vpn-watchdog.sh` 以前写死，现在也读它） |
| `RUNLOCK_WAIT` | `900` | `vpn-watchdog.sh` 重建前等 media-agent 运行锁的秒数，等不到这一轮不重建（退出码 75） |
| `WATCHDOG_COMPOSE_TIMEOUT` | `180` | `vpn-watchdog.sh` 的 `docker compose up` 最多等几秒，超时中止、记 `ERROR`、退出码 1（与 `rescue.py` 一致） |
| `RESCUE_LOCK_WAIT` | `900` | `rescue.py start` / `stop` 切换前等运行锁的秒数，等不到不切换（退出码 75） |
| `~/gluetun/.env.vps` 里的 `WIREGUARD_ENDPOINT_IP` | — | 救援隧道对端，`rescue.py` 用来核对出口 IP |

`.env` / `.env.vps` 由 `.gitignore` 挡在仓库外——它们装着 WireGuard 密钥和
服务器地址，不该进版本控制。`rescue.py` 要用 venv 里的解释器跑
（`~/media-agent/.venv/bin/python deploy/rescue.py …`）：系统的 `/usr/bin/python3` 没有 httpx。

## 重建容器与 media-agent 的协调（critic N17）

两个脚本都会 `docker compose up -d --force-recreate`，qBittorrent（与 gluetun 共用网络命名空间）跟着被拆掉重建。

- **重建之前拿 media-agent 的运行锁**（`state/run.lock`，与 media-agent、`deploy.sh` 同一把 flock）：一轮 `run` 正在跑
  就等它结束（一轮约 2 分钟），最多等 900 秒；等不到就这次不重建 / 不切换，退出码 75。`vpn-watchdog.sh` 在锁里把自己
  重跑一遍（等锁期间隧道可能自己好了，重跑会先重新看健康状态），`vpn-watchdog.log` 里记 `WAIT` / `SKIP`。
  拿着锁时往 `run.lock` 里写一句自述（`pid=… cmd=vpn-watchdog.sh … since=…`，退出时清掉），被挡住的 `run` 与健康报告
  打印的就是它；docker 的每次调用都有上限（compose 180 秒、inspect / info 30 秒），Docker / OrbStack 卡住时不会无限期
  拿着锁把每一轮 `run` 挡成 75。`rescue.py` 只在切换的那几分钟里拿锁（`auto` 等下载的几个小时不拿）。
- **救援期间 media-agent 自己暂停**：`rescue.py start` 写下的 `~/gluetun/.rescue-active` 在，`run` / `apply` 就以 75 结束、
  健康报告 warn（`media_agent/pause.py`）；`stop` 删掉它之后自动恢复。`stop` 拿不到锁时标记留着——安全一侧，稍后再跑
  一次 `rescue.py stop`。

### 生产上的副本要手动同步

生产跑的是 **`~/gluetun/` 下的拷贝**（`vpn-watchdog.sh` 由它自己的 launchd 任务每 6 小时跑，`rescue.py` 人手动跑），
`deploy.sh` 不碰它们。仓库里的这两个文件改了，部署完 media-agent 之后手动同步一次：

```sh
cd ~/media-agent
diff -u ~/gluetun/vpn-watchdog.sh deploy/vpn-watchdog.sh    # 先看清楚差在哪（生产上有没有手改过）
diff -u ~/gluetun/rescue.py deploy/rescue.py
cp deploy/vpn-watchdog.sh ~/gluetun/vpn-watchdog.sh
cp deploy/rescue.py ~/gluetun/rescue.py
/bin/bash -n ~/gluetun/vpn-watchdog.sh && echo ok            # bash 3.2 下能解析
```

`rescue.py` 需要 media-agent 里有 `media_agent/runlock.py`（v0.2.0 起都有）。先部署 media-agent、再同步脚本：
反过来的话旧版 media-agent 不认救援标记（不会暂停），但锁照样起作用。

## rescue.py 的分享率策略

平时（ExpressVPN）**不限制**：带宽不值钱，随便做种。
救援中（自有 VPS）**上限 1.0，达标即暂停**：VPS 流量要花钱。

退出救援时要叫醒的是「现在停着、但进入救援前没停」的种子——这样既覆盖
`start` 时我们主动暂停的那批，也覆盖救援期间因达到 1.0 被 qBittorrent
自己暂停的那批，而用户手动停的不动。只认前者会让后一批永远停着没人管。
