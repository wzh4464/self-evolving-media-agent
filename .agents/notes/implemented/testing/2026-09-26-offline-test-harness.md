# 离线测试基座：让"全自动删改"在进生产前先在假库上跑一遍

**日期**: 2026-09-26
**状态**: implemented / testing
**触发**: 整改前只有 3 个脚本式测试、pytest 收集到 0 个；其中一个还要登录生产 qBittorrent

## 为什么需要

这个 agent 在生产上**全自动**移动、改名、隔离文件。过去一个多月的事故
（幻影重复删掉唯一真文件、合集种子整季塌缩、抓取记账全记 failed……）
都是在生产库上第一次暴露的——因为没有任何地方能在不碰真库的前提下，
把"扫描 → 全量规则 → 执行 → 回退"完整跑一遍。

更隐蔽的问题是**本项目处处吞异常**，而且吞得有理由（单条规则崩溃不能拖垮
整轮）。这让"写个假对象跑一下"变得危险：假对象少实现一个方法，被测代码
照样跑完，断言看到的只是"什么都没发生"，测试就绿了。2026-09-26 的抓取
记账事故正是这个形态——12 次抓取全记 `failed`，外面看一切正常。

## 做了什么

`tests/harness/`：

- **FakeQbit**：`QBitClient` 被用到的 13 个方法，背后是临时目录里的真文件。
  语义按生产 v5.2.3 实测：`renameFile` 不改显示名、`.!qB` 跟着改；
  `content_path` 单文件 = 文件、Original = 根目录、**NoSubfolder = save_path**；
  `root_path` 按上游 `findRootFolder`（单个 `root/file` 条目也有根）；没有元数据的
  metaDL 种子两者都是空串（上游 torrentimpl.cpp:556-578，2026-09-26 审查后对齐——
  以前"一集装在文件夹里"报空 root_path，死种回退在基座里丢掉文件夹，而生产是对的）；
  标签排序 `", "` 连接；未知 hash 的 `files()` 抛与真客户端同文本的 404；
  优先级 0 文件留在盘上。可注入故障（某个 hash 的 `files()` 超时等）。
  解析 .torrent 时种子名与每个路径元素里的 `/` 换成 `_`（libtorrent 的
  `sanitize_append_path_element`，按源码，未在生产上逐条核对）：FakeWeb 用 Mikan
  站点标题当单文件名，标题常带 ` / `，以前在 FakeQbit 里成了"文件夹/文件"条目——
  生产上不会出现，而抓取后改名保留文件夹层（N15）之后它会让测试失真（2026-09-26）。
- **FakeWeb**：只替换 `urllib.request.urlopen` 一处——三条网络路径都在调用时
  查它（grab.py 绑的是 `_http_get` 的引用，只补 `_http_get` 不够）。
- **FakeProbe**：只替换 `probe._run`——`builtin` / `purge` 都按名字导入了
  `probe` / `duration`，补 `probe.probe` 传不到它们。合成 ffprobe JSON，
  于是真实解析逻辑照跑；按文件头身份认文件，改名、进隔离区后结果不丢。
- **FakeTMDB / FakeAB / FakeLLM**；AutoBangumi 的库用**真的** `AutoBangumiDB`
  跑在临时 sqlite 上，docker 换成只记参数的脚本。
- **LibraryBuilder**：几行声明剧、季、种子、磁盘文件（稀疏文件，大小精确）、
  sidecar、订阅；`lib.cycle()` 一轮 = 新 Context → 扫描 → 全量内置规则 → 执行（只跑一次）；
  `lib.loop()`（2026-09-27）= 一轮 `run` 的迭代到不动点：一个 Context、一个执行器，
  `converge.run`，返回的 `Loop` 多一个 `outcome`（每次迭代、停在哪、待做、`oscillation`）。
- **Tripwire**（conftest 自动启用）：没路由的 URL、直连网络、起子进程、
  调用未建模方法、Executor 的 failed 审计、Registry 吞掉的检测器异常、
  含「失败」的日志——测试结束时有未声明的就判失败。

## 几条不能省的约束

1. **永远不走 `cli.build_context`**：它真的会 HTTP 登录。直接 `Context(cfg, qbit=…)`。
2. **每轮一个新 Context，并清进程级缓存**（`probe._CACHE`、`builtin._OFFSET_CACHE`）：
   `ctx._tfile_cache` 在同一个 Context 上永不失效，复用会看到改名前的幽灵路径。
   这正是 `cmd_run` 在 apply 后同 ctx 重扫时的真实缺陷，测试不能复制它。
3. **run_id 显式给**：默认 run_id 只精确到秒，同一秒两轮会被回退当成一批。
4. **`PROJECT_ROOT` 打到临时目录、不读 `.env`**：`_load_dotenv` 写进 `os.environ`
   的东西 monkeypatch 撤不回来，而仓库里的 `.env` 有真凭据。
5. **日期相对今天**：代码直接调 `date.today()`，不引入冻结时钟。
6. **磁盘剩余空间固定为充足**（2026-09-26 隔离区容量闸加入时补）：`disposal.free_bytes`
   （`statvfs`）在 conftest 里固定返回 1 PB——容量闸（`MIN_FREE_GB`）与隔离前的空间检查不能随
   开发机 / CI 的磁盘而变。测空间不足的用例自己再 monkeypatch 它。另外，**不要在测试里调
   `monkeypatch.undo()`**：它会连 conftest 的隔离（`PROJECT_ROOT`）一起撤掉，之后的写入落进仓库
   的 `state/`；只撤自己设的那一处就重新 `setattr` 回原值。

## 依赖与部署的兼容

`uv add --dev pytest` 把 `uv.lock` 从 revision 1 升到 revision 3。已核实生产的
uv 0.7.2 能读：`uv lock --check`、`uv sync --frozen`、`uv sync --frozen --no-dev`
在 Python 3.12 下都通过，且不改写锁文件。

**遗留风险**：launchd 现在跑的是不带 `--frozen` 的 `uv run`，它会同步默认依赖组
（含 dev），部署后第一轮会从 PyPI 装 pytest。部署前应把 plist 改成
`uv run --frozen --no-dev`（或直接调 `.venv/bin/media-agent`），见部署阶段的整改。

## CI（`.github/workflows/tests.yml`，8edf092 引入，本节补记其取舍）

push 到 `main` / `phase/**` 与 PR 时跑。两个任务：

- **pytest 矩阵**，每条腿都有它存在的理由：
  - Ubuntu × Python 3.12：生产解释器（zihan_air 是 CPython 3.12）。**只有这条腿装
    ffmpeg**，`@pytest.mark.ffmpeg` 的一致性测试（FakeProbe 与真 ffprobe 的输出对得上）
    只在这里真跑，其它腿自动跳过——装 ffmpeg 要一分多钟，一条腿证明就够。
  - Ubuntu × 3.14：开发机的解释器。
  - macOS × 3.12：生产媒体卷是大小写不敏感的 APFS，路径比较与 ext4 不同；
    而且 `tests/test_deploy_scripts.py` 必须在 macOS 自带的 `/bin/bash` 3.2 与 BSD 工具上跑
    （`deploy.sh` 在生产上就是这么跑的）。
- **prod-uv**：用生产机上的 uv 0.7.2（`uvx --from 'uv==0.7.2'`）做 `lock --check`、
  `sync --frozen --no-dev`、`run … --help`，最后 `git diff --exit-code uv.lock`。
  新 uv 写出的锁文件老 uv 读不懂，部署后第一轮就起不来；这个任务专门挡这一条。
  **生产机的 uv 升级了才改这里的版本号**，而且两边一起改。

**action 一律钉到 40 位提交 SHA，行尾注明 `# vX.Y.Z`**（`tests/test_ci_workflow.py` 守着）。
最初写的是 `astral-sh/setup-uv@v10`：setup-uv 从 v8 起不再发布浮动的大版本 tag，上游只有
`v10.0.0`…`v10.2.0`，`@v10` 解析不到，**每个任务在第一步就失败**——本地测试全绿，
CI 一次都没真跑过。升级 action 时先
`git ls-remote https://github.com/<owner>/<repo> 'refs/tags/vX.Y.Z*'` 拿到 SHA
（没有 `^{}` 行说明是轻量 tag，SHA 就是提交；有的话取 `^{}` 那一行），再改工作流。

## 怎么用

见 `tests/harness/__init__.py` 的 fixture 表；基座自身的语义断言在 `tests/test_harness.py`；
五个真实流程的范例在 `tests/test_e2e_smoke.py`（改名、判重腾空、回退、抓取、订阅修复）。
跑法：`uv run pytest`；生产只读核对：`MEDIA_AGENT_LIVE=1 uv run pytest -m live`（zihan_air）。
