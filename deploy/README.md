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
| `state/` | 只在生产 | `audit.jsonl`（回退的依据）、`audit.fallback.jsonl`（审计写不进主文件时的转写，回退一起读）、`purge.jsonl`、`cache.sqlite3`、`trash/`（隔离区）、`run.log`、`run.lock`、`deploy.lock`、`deploy.history`、`deploy.log`、`backups/`、`harvest/`、`ab_mode.json`（`media-agent ab-mode` 写的 AutoBangumi 模式，见「AutoBangumi 的模式」） |
| 各番目录里的 `.media-agent.json` | 媒体根下 | 每部番的用户意图，不归部署管，见下节 |

## 用户意图放在哪

- **每部番的意图**——"只保留 X 版"（`require_any`）、季内集号换算（`season_offsets`）、
  备注、`mikan_id`、钉住的字段（`pinned`）、TMDB 身份（`tmdb_id`，代码只在没有时填一次）——在媒体根下
  各番目录的 `.media-agent.json` 里。它们是状态，不入库、部署从不碰；要进备份。同一个文件里的
  各季进度、来源、别名是 media-agent 算的，每轮会重写；人写的字段它从不覆盖（字段归属见
  `media_agent/sidecar.py`）。文件改坏了（解析不了）media-agent 不会覆盖它：拷一份
  `.media-agent.json.corrupt-<时间>`、每轮健康报告 warn，修好或删掉之后下一轮照常写。
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
5. **launchd。** 两个任务（v0.6.0 起）：每 6 小时的 `com.zihan.media-agent`（`run`）与每 30 分钟的
   `com.zihan.media-agent-grab`（`grab`），各自一份 `deploy/<label>.plist`。每一份都是 tag 里的与已装的不同才重装
   （`plutil -lint` → `launchctl bootout` → `bootstrap gui/$(id -u)`），没变的不动；**tag 里没有抓取任务**（回滚到
   v0.6.0 之前）就把它卸掉——留着它每 30 分钟调一个那个版本里不存在的子命令。任何一份装不上，这次动过的都装回部署
   前的那份（部署前没有的卸掉），代码也一起退回。重装会让 `StartInterval` 从那一刻重新计时。
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
  记录（`ok / converted / reverted / revert-failed`）正是这个 commit 的 `ok` 或 `converted`、**而且装着的
  launchd 任务与 tag 里的一字不差**时才短路，否则照常走一遍，依赖、测试、launchd 配置都重新落实（对完好的目录
  这一遍是幂等的）。

**从 v0.5.x 升到 v0.6.0（第一次有抓取任务）要跑两遍。** 部署跑的是**工作区里当前那一版**的 `deploy.sh`
（先复制一份再执行），v0.5.x 的脚本只认主任务的 plist：第一遍把代码切到 v0.6.0、装好主任务，抓取任务没装。
再跑一遍同一个 tag（这时工作区里已是 v0.6.0 的脚本）：代码不变，它发现抓取任务没装上、不短路，把它装上。

```sh
deploy/deploy.sh v0.6.0      # 用 v0.5.x 的脚本：代码切过去，只装主任务
deploy/deploy.sh v0.6.0      # 用 v0.6.0 的脚本：装上 com.zihan.media-agent-grab
launchctl print gui/$(id -u)/com.zihan.media-agent-grab | grep -E 'state|run interval'
```

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
- **AutoBangumi 在订阅模式时**，先 `media-agent ab-mode full` 再回滚到 v0.6.0 之前：旧代码不认 `state/ab_mode.json`，会把一个
  已经停了改名的 AB 当成还在改名（`Bangumi` 分类永远让位、新订阅补的集没人改名），还会在修订阅时叫它刷新。
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
| `run`、`grab`、`apply`、`rollback`、`repair`、`evolve`、`subscribe`、`purge --apply`（含 `--dry-run`）、`ab-mode subscription\|full` | `scan`、`diagnose`、`runs`、只预演的 `purge`、`ab-mode`（show）与 `ab-mode … --dry-run` |

- 拿不到锁最多等 10 秒（`run` 等 300 秒，见下），然后打印持有者（pid、命令、开始时间）、以**退出码 75** 结束，
  什么都不做。launchd 的一轮撞上部署或手动操作就是这样：`last exit code = 75`，6 小时（抓取是 30 分钟）后再来。
- **`run` 与 `grab` 每 6 小时撞一次**：两个任务的 `StartInterval`（21600 / 1800）都从加载那一刻起计时，每 6 小时
  同一秒起来。所以 `run` 等锁最多 300 秒（`runlock.RUN_WAIT`，抓取一般一两分钟就完）；`grab` 只等 10 秒——
  绝不为一轮 `run` 等上几分钟，30 分钟后它自然再来。
- 进程被 kill 锁自动释放，不会留下要人去删的锁。**不要删 `run.lock` 文件**——
  删了之后持有者锁住的是一个没有名字的 inode，下一个进程会拿到另一把锁。
- `deploy.sh` 用 `/usr/bin/lockf -k`（Linux 上是 `flock(1)`）拿同一把锁，切换期间一直持有。
- 手工维护时想让 agent 暂停：`touch ~/media-agent/state/PAUSE`（里面可以写一句为什么），`run` / `apply` / `grab` 就以 75 结束、健康报告 warn；维护完 `rm` 掉。VPN 救援期间（`~/gluetun/.rescue-active` 在）自动暂停。要在自己干活的那几分钟里连手动命令也挡住，就自己拿着锁：
  `/usr/bin/lockf -k ~/media-agent/state/run.lock zsh`（退出这个 shell 即释放）。
- `vpn-watchdog.sh` 与 `rescue.py` 重建 qBittorrent 容器之前同样拿这把锁（critic N17）：最多等 900 秒
  （`RUNLOCK_WAIT` / `RESCUE_LOCK_WAIT`），等不到就这次不重建、退出码 75。见下文「vpn-watchdog.sh / rescue.py」。

## launchd

`com.zihan.media-agent.plist` 直接执行 `~/media-agent/.venv/bin/media-agent run`，
**不经 `uv run`**：`uv run` 每轮都会按 `uv.lock` 同步环境，锁文件一变就在凌晨联网装依赖，
PyPI 不通这一轮就起不来。依赖只在部署时装。周期 21600 秒、`RunAtLoad false`、`Nice 10`、
`LowPriorityIO`，stdout / stderr 追加到 `state/run.log` / `state/run.err.log`。每一行带时间与批次 ID，每轮以「run 开始：批次 …」一行开头；两个文件超过 5 MB 时由 `run` 自己先拷贝再截断地轮转成 `.1` … `.5`（`media_agent/runlog.py`：launchd 持有描述符，不能改名；不需要 newsyslog）。
`com.zihan.media-agent-grab.plist`（v0.6.0 起）执行 `~/media-agent/.venv/bin/media-agent grab`：周期 1800 秒，
其余（`RunAtLoad false`、`Nice 10`、`LowPriorityIO`、不经 `uv run`）与主任务相同；输出追加到 `state/grab.log` /
`state/grab.err.log`，同样带时间与批次 ID、超过 5 MB 由 `grab` 自己轮转。它只补缺的集、接手 AB 里新订的番、给刚抓的
改名 / 判重收尾，其余治理仍是 6 小时一轮的 `run`（`media_agent/grabmode.py`）；健康报告在 `state/health/grab/`
（`media-agent health --grab`），通知只为抓取相关的事发（崩溃、整批拒绝、审计没写全、抓取动作失败），"锁被 run 占着"
不发信。退出码与主任务同一张表。

两份 plist 都由 `deploy.sh` 在变化时自动重装；手工重装（抓取任务把 label 换成 `com.zihan.media-agent-grab`）：

```sh
launchctl bootout gui/$(id -u)/com.zihan.media-agent
cp ~/media-agent/deploy/com.zihan.media-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zihan.media-agent.plist
```

临时停掉抓取、保留 6 小时的 `run`：`launchctl bootout gui/$(id -u)/com.zihan.media-agent-grab`。plist 文件还在
`~/Library/LaunchAgents/`，下次登录（重启）时 launchd 会重新加载它；部署只比文件，文件没变就不会替你装回来——恢复用
上面的 `bootstrap` 那一行。想连 `run` 一起停就用 `state/PAUSE`。

`launchctl list | grep media-agent` 的 last exit code（每轮的详情在 `state/health/<批次 ID>.json`，`media-agent health` 看最近一轮）：

| 退出码 | 含义 |
|---|---|
| 0 | 正常（健康报告 ok 或 warn；warn 的原因见 run.log 末尾的「健康」一节或 `media-agent health`） |
| 1 | 这一轮崩了（异常冲出）：完整 traceback 在 run.err.log，健康报告照写 |
| 3 | 降级、整批拒绝改动（qBittorrent 不可用、读不全，或种子数比上一轮骤降而审计解释不了——确认是人为删除的用 `media-agent health --accept-torrent-count`），什么都没改 |
| 4 | 改动照常做了，但有审计记录没能原样写进 `state/audit.jsonl`：写不进去的（磁盘满、权限……）已转写到 run.err.log 与 `state/audit.fallback.jsonl`，`rollback` / `runs` 会一起读——先腾空间；值序列化不了的已按字符串降级写进 `audit.jsonl` 本身（run.log 末尾写明是哪一种）——多半是代码 bug |
| 5 | 这一轮跑完了，但健康报告 critical：隔离区处置之后媒体卷剩余仍低于 `MIN_FREE_GB`——要人腾空间 |
| 75 | 另一个进程持有运行锁，或维护暂停中（VPN 救援进行中 / 有 `state/PAUSE`），这一轮什么都没做 |

## AutoBangumi 的模式

v0.6.0 起有 `AB_MODE`，默认 `full`——**部署这个版本什么都不变**，直到人切。`subscription` = AutoBangumi 只当订阅的前端：
它的 RSS 线程（拉 RSS、下载）与改名线程都关掉，WebUI 与订阅照旧；抓取（`grab`，每 30 分钟）与改名全归本项目。为什么、
改了什么见 `media_agent/abmode.py` 与 `.agents/notes/implemented/architecture/2026-09-27-ab-mode.md`。

### 切换之前

1. 至少跑完一轮 v0.6.0 的 `run`：`ab-adoption` 把 AB 订阅行上的集号偏移迁进各番的 sidecar（生产上只有 AB 37 一条：
   《超超超超超喜欢你的100个女朋友》第三季，-24，部署后第一轮 `run` 提议一次 `adopt_episode_offset`）；`media-agent health`
   里没有 `ab_subscription_unmapped` / `ab_subscription_moved`——这两种订阅 AB 一停就没人接。
2. 抓取任务在跑：`launchctl print gui/$(id -u)/com.zihan.media-agent-grab | grep state`，`media-agent health --grab` 最近一轮
   不是 critical。
3. 两边此刻各认什么：`media-agent ab-mode`（= `show`）。预演：`media-agent ab-mode subscription --dry-run`。
4. （可选）留一份 AB 的配置：`cp <ab-config>/config.json ~/ab-config.json.bak-$(date +%F) && chmod 600 ~/ab-config.json.bak-*`
   ——里面有密码。切换本身不需要它（逆操作记在审计里）。

### 切换

```sh
cd ~/media-agent && .venv/bin/media-agent ab-mode subscription
```

它做的：读 AB 的**整份**配置（`GET /api/v1/config/get`，密码打码）→ 只把 `rss_parser.enable` 与 `bangumi_manage.enable`
改成 false → **整份**发回（`PATCH /api/v1/config/update`；AB 按整个对象解析，漏掉的段退回默认值——手工改时也绝不要只发这
两项）→ `GET /api/v1/restart`（程序重启：停掉四个后台任务、按 config.json 重起；WebUI 不停；**不是** `docker restart`，容器
里 qb_downloader 的补丁不受影响）→ 最多等 6 分钟它回来、读回开关核对 → 写 `state/ab_mode.json`（盖过 `.env` 的 `AB_MODE`），
并记下核对用的基线（每个 rssitem 的 `last_checked_at`、已有的订阅 id）。拿运行锁：正在跑的 `run` / `grab` 跑完才切，下一轮
按新模式行事。审计里一条 `set_ab_mode`，批次 ID 印在输出里。

| 退出码 | 含义 |
|---|---|
| 0 | 切成了（或本来就是） |
| 1 | 没核对上（unknown：重启之后等不到它回来 / 读回的开关不对）或失败——输出里写着怎么核对、怎么退回；本项目的模式没动 |
| 2 | 配置错误（`AB_MODE` 写错、`state/ab_mode.json` 坏了） |
| 3 | AB 接口连不上 / 认不出它的配置：什么都没动（配了 `AB_CONFIG`——AB 的 config.json 在宿主上的路径——就只读它，说一句看到的开关） |
| 75 | 运行锁被占着 |

### 核对（切换后 15–30 分钟看一次，隔天再看一次）

- `media-agent ab-mode`：两边都是 subscription、「切到 subscription 之后 AB 没再拉过 RSS」、「订阅动作之外 AB 没加过种子」；
  有问题退出码 1。
- 每一轮 `run` / `grab` 的健康报告多一行 `AB      subscription（state，自 …） · 拉 RSS 0 · 订阅之外加种 0 · 订阅补集 N`；
  warn `ab_still_polling` / `ab_added_outside_subscribe` / `ab_mode_unverified`（`run` 的会发信）。
- 只读看 AB 的日志 `log.txt`：`Program running.` 之后不再有 `[Engine]` 与 ` >> ` 行；qBittorrent 里不再出现订阅之外的新
  `Bangumi` 种子。
- **别看 `/api/v1/status`**：不管哪几个线程起了它都说 true。
- 订一部新番试试：AB 的 WebUI 里订阅 → 它当场把已发布的集加进 qBittorrent（`Bangumi` 分类；新订阅那一刻还没有 id，不带
  `ab:` 标签）→ 30 分钟之内 `grab` 建目录（需要的话）、登记订阅、把这些种子交接到剧名分类并改名，之后的集由 `grab` 抓；
  健康报告把这一批计进「订阅补集」，不报警。

**`ab_still_polling`**：`last_checked_at` 又变了 = AB 又拉了一次 RSS（拉了就可能下载）。`media-agent ab-mode` 看它的开关：
开着（有人在 WebUI 里打开了）→ 再跑 `media-agent ab-mode subscription`；关着却还在拉 = 程序没重启成 → 同一条命令会再重启一次。
有人在 WebUI 里点了「刷新」也会这样（刷新不看开关、会下载），那一次核对一下 qBittorrent 里多了什么，然后同样再跑一次
`media-agent ab-mode subscription`。核对是按切换时记的基线比的：**不再跑这一次，这条 warn 就一直在**，之后真的又拉了也分不出来。
两边都已是 subscription 时，这条命令只在核对有问题（没有基线、拉过 RSS、订阅之外加过种子、读不全）时动手：不再 PATCH，
重启一次、核对、重新记基线；核对没问题就说「已经是」、什么都不做。
**`ab_added_outside_subscribe`**：切换之后 AB 加的种子不是新订阅那一刻补的那一批（启发式：新订阅的保存路径下、第一个种子
之后 1 小时之内的才算）——RSS 线程、刷新、对老订阅点了「收集」；核对过之后同样再跑一次 `media-agent ab-mode subscription`
重新记基线。**`ab_mode_unverified`**：模式来自 `.env` 的 `AB_MODE`、没经命令切过，或切换那一刻 AB 库读不了（核对不了 AB 关没
关），或这一轮读不到 AB 库——用命令切一次（开关已关的只重启、核对、记基线）/ 看 `AB_DB`。

**没核对上（unknown）**：AB 的 config.json 也许已是新开关、线程还是旧的（要到下一次重启才换）；本项目的模式没动。AB 回来之后
再跑一次同一条命令——开关已对的不再 PATCH，只重启、核对、记录。要退回按下一节。

### 回退

任选其一：

- `media-agent ab-mode full`：两个开关打开、重启、核对，`state/ab_mode.json` 记成 full。
- `media-agent rollback --run <切换那一次的批次 ID>`：开关改回切换之前的样子、状态文件还原成切换之前的（原来没有就删）。切换
  之后有人改过开关、或又切过一次，这一步跳过并写明——那时用上一条。
- AB 的 WebUI 设置里打开「RSS 解析」「番剧管理」、点应用（WebUI 会重启程序）——然后 `media-agent ab-mode full`，让本项目
  也认 full（或删掉 `state/ab_mode.json` 回到 `.env` 的 `AB_MODE`）。两边不一致时 `media-agent ab-mode` 退出码 1、说出来。

**回退之后会有一波补下载。** AB 的 `torrent` 表按 **URL** 判新：停着期间 feed 里发布的条目全算新的，打开之后第一次拉 RSS
（15 分钟之内）就一口气下——大多是本项目已经抓过的集。它们落进 `Bangumi` 分类、AB 改名；`full` 下判重对 `Bangumi` 让位，
`run` 的分类交接之后（一轮之内的下一次迭代）判重清掉重复，输家进隔离区、按判重的处置规则保留期后删。停得越久，这一波越大；
只是临时切回的话，预计多出"停着那几天 × 在追的番"那么多个种子。**不要**为了少下一些在 AB 里停用订阅：停用把订阅行标成
`deleted=1`，本项目只读 `deleted=0`，这条订阅的接手、集号偏移、新季登记都跟着没了。

**`AB_MODE` 与状态文件。** `state/ab_mode.json`（命令写的）> `.env` 的 `AB_MODE` > `full`；部署从不碰 `state/`。状态文件坏了，
所有命令以退出码 2 拒绝启动（「配置错误：…ab_mode.json…」）：删掉它、用 `media-agent ab-mode` 核对两边，再切一次。代码回滚到
v0.6.0 之前，先切回 full（见「回滚」）。

## 部署脚本自己的测试

`tests/test_deploy_scripts.py` 在临时目录里模拟生产目录（替身 uv / launchctl），覆盖闸门、
暂存失败、原地失败自动退回、plist 装不上（两个任务各自、一起退回）、只重装变了的任务、tag 里没有抓取任务时卸掉、
同一个 tag 再部署补上缺的任务、锁、转换与撤销；CI 的 macOS 腿用的正是生产机上的
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
