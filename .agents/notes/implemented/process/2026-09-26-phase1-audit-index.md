# 整改第一阶段的审计编号索引

**日期**: 2026-09-26
**状态**: implemented / process
**触发**: 审查发现 CHANGELOG、代码注释、plist、部署手册、Agent Notes 与提交说明里有 55 处
`critic N1`、`testinfra B2`、`LAT-01`、`runloop §8c` 这样的编号，而定义它们的是整改前那轮
只读测绘的调研报告——那些报告没有入库，也不会入库。公开仓库的读者、以后的维护者都查不到。

这份笔记让每个编号都能在仓库里查到：一句话说明它是什么、对应的日期 / 批次 ID（如有）、
在哪个提交里处理。**不含**任何 IP、凭据、个人路径；批次 ID 就是 `audit.jsonl` 里的 `run_id`。
以后新增编号，要么在这里补一行，要么在引用处直接写日期与批次 ID，不要只留一个裸编号。

## critic：整改计划的完整性评审

| 编号 | 是什么 | 处理 |
|---|---|---|
| N1 | 回退 `restore_from_trash` 把空的 `trash_path` 变成 `Path('.')`，`shutil.move('.', dst)` 退回 `copytree(当前目录 → 媒体库)` 再 `rmtree(当前目录)`。根因：`_op_trash` 先删种子记录、后查文件是否存在。生产上 6 条这样的记录：20260830T132317、20260908T022758 ×3、20260908T143348、20260920T170126 | 8f66cab（回退校验参数）、bc8d5a3（隔离前先核对文件） |
| N2 | qBittorrent **部分**读取失败：`files()` 的任何错误被缓存成空列表，那个种子的文件变成"纯本地文件"，改名走文件系统、隔离跳过种子，而且无声无息 | 5fde004 |
| N3 | `rollback` / `repair` / `purge --apply` 不看 qBittorrent 在不在：逆改名退化成 `mv`，`repair` 用文件系统搬活种子的文件，purge 丢掉种子证据 | 5fde004；qBit 在线时的目录级搬运见 0fe98b2 |
| N5 | 演进规则（LLM 提议、影子验证后上线的 DSL）可以带 `trash` / `retag` 等动作、参数由模型选，上线后不再复核——一条未经人审的"LLM → 改名 / 删除"通路 | ae4a8bb；删除的执法点并入删除关口 680962b（`gate.screen`，trash 与 drop_torrent） |
| N6 | "这个路径是否已被另一个活种子声明"应当是所有写路径共用的一道闸；`relink_torrent` 只按大小匹配，可能造出两个种子争一个文件 | 第 1 阶段 2577c43 给改名加了 `_claimants`；第 2 阶段换成共用原语 1e7897d，接到改名 9dcf436、抓取后改名 8a0dec9、relink f2c9be8、回退 be0eae6 / 1726d3d、目录改名 4be6339 / 64a2f5b |
| N7 | `Finding.key()` = `(kind, path)`：同一目录两个死种的 `content_path` 相同，第二条被去重吞掉 | 3ef6825；删除关口按目标（路径 + hash）复核、不按 key，680962b 起 |
| N8 | 隔离区与媒体在同一个 APFS 容器（约 94% 满）：隔离不腾空间，跨卷搬运是先拷后删，磁盘满时搬到一半失败 | 第 2 阶段：容量闸 `MIN_FREE_GB` 86b1cc5；搬进 / 搬出隔离区先看放不放得下 1a3e5c6。见 `architecture/2026-09-26-quarantine-disposal.md`。余项（写审计本身在磁盘满时抛异常，冲出 `apply()`、整轮连隔离区处置一起中止）：第 3 阶段 105aeee；搬到一半失败的结局按盘上状态认 d6d7d6b |
| N9 | 每个检测器各开一个 sqlite 连接（默认 5 秒忙等）：第二个进程并发时"database is locked"在检测器里抛出、被 Registry 吞掉，诊断静默地少一截（判重在出错之后的桶全丢） | 第 3 阶段：`Registry.errors` 记下每个被吞的异常，见 `architecture/2026-09-26-run-health.md` 第 4 节；并发本身由运行锁 0b5a02e 排除 |
| N10 | 批次 ID 只精确到秒，同一秒起的两个执行器共用一个回退单元（launchd 上 media-agent 与 vpn-watchdog 周期同为 21600 秒） | 0b5a02e |
| N11 | 没有结构化的发现历史：无动作的发现只以文字进 run.log；没有路径的发现去重键退回 `(show, summary)`，摘要里嵌着计数，跨轮认不出同一个问题——"卡住"检测无从谈起 | 第 3 阶段：发现历史与稳定指纹（`history.py`），见 `architecture/2026-09-26-run-health.md` 第 1 节 |
| N12 | 回退只写一条汇总：逐步还原了什么没有审计，健康摘要看不见回退改了什么 | 第 3 阶段 8c901f5（逐步记录；回退记录不带逆操作，回退不能再回退）。见 `architecture/2026-09-26-honest-audit.md` 第 8 节 |
| N15 | 抓取后的即时改名 `rename_single_video` 算目标名时丢掉条目的文件夹层（`_op_rename` 保留）：409 撞上一个已有的 Original 布局种子时，文件被挪到 save_path 根下 | 8a0dec9，见 `architecture/2026-09-26-path-claims.md` 第 4 节 |
| N17 | `rescue.py` / `vpn-watchdog.sh` 重建 qBittorrent 容器时不看任何锁或维护窗口；运行锁应覆盖 `purge --apply`、`rollback`、`repair` 与手动会话 | 部分：0b5a02e（运行锁）；两个脚本仍不看锁 |
| §2（对 executor 调研的更正） | executor 调研以为 `list_runs` 把"已回退"标在 `rollback-of-X` 上、`rollback --last` 会再选 X；其实汇总里的字面量 `rollback-of-…` 被随后的 `**result` 覆盖回原批次号，一直标对了（生产 4 条历史汇总都是原批次号） | 第 3 阶段 8c901f5 明写汇总的 `run_id`，逐步记录另用 `rollback-of-…` |
| §3.5 | 审计的状态要向后兼容地扩展（约 9.6k 行、好几代格式）：执行器报告分桶 `{...}[status]` 遇到新状态就 KeyError；`_read_audit` / `list_runs` / `purge._audit_by_trash_path` / `find_failure_patterns` 都读状态 | 第 3 阶段 ba842c5（`unknown` 与各读者），契约见 `architecture/2026-09-26-honest-audit.md` 第 10 节 |
| §3.6 | 运行锁不能等到后面的阶段：`purge` / `rollback` / `repair` / 手动会话今天就与 `run` 竞争 | 0b5a02e |
| §3.7 | 锁文件变更与 launchd：plist 用 `uv run`，每轮按 `uv.lock` 联网同步，引入 pytest 后凌晨那轮就要装包；先把 plist 改成直接跑 `.venv/bin/media-agent` | d504b3c |
| §3.8 | 改成按 git tag 部署之前先冻结演进：演进器往仓库目录里写规则 / 笔记，会被部署的漂移闸门拦下 | 653aff2 |

## testinfra：离线测试基座的调研（原型发现的 bug）

| 编号 | 是什么 | 处理 |
|---|---|---|
| B1 | 死掉的 NoSubfolder 多文件种子，`content_path` 就是整个 `Season N` 目录：死种处置会把整季（别的种子的文件、封存集位）搬进隔离区，目录的 `st_size` 还让体积配额失效（生产上有 10 个这种布局的种子，尚未触发） | 3ef6825 |
| B2 | 同一批次里种子已被判重作废，后面对它的改名去问 `files()` 得 404，记成 `failed`（生产 5 次，即 LAT-05） | af2cbb3；测试补强 7561bde |
| B3 | `ctx._tfile_cache` 在 Context 的整个生命周期里不失效：同一个 ctx 重扫会看到改名前的旧文件名（幻影重复 + "未改名"） | d40e506 |

## LAT：从生产审计里挖出来的潜伏问题

| 编号 | 是什么 | 处理 |
|---|---|---|
| LAT-01 | 2026-09-19/20 qBittorrent 登录超时的运行照常改动媒体库：run 20260919T225410 把归种子 d08f05a7 所有的「朱音落语 S01E12.mp4」以 `torrent_hash ""` 移进隔离区；下一轮 20260920T170126 又删了那个种子的记录（`freed 0`） | 5fde004（不可用 / 读取失败）、2f4fad8（返回空列表）；留下的幻影见 2577c43 |
| LAT-02 | 回退一条 `trash_path` 为空的记录会搬走当前目录——即 critic N1 的回退一侧 | 8f66cab |
| LAT-05 | 对本批次早先已删掉种子的文件改名，记成 `failed`（HTTP 404）：20260909T024309、20260911T091146 ×2、20260916T102034、20260918T145907——即 testinfra B2 | af2cbb3 |

## runloop：运行循环的调研

| 编号 | 是什么 |
|---|---|
| §4 | 输出与日志：发现与汇总进 stdout、日志进 stderr，launchd 追加到 `state/run.log` / `run.err.log`；两份日志都没有时间戳、批次号、轮次分隔，也不轮转（生产 run.log 1.8 MB / 159 轮）；没有动作的轮次不留任何审计 |
| §5 | launchd 的实际配置（每 21600 秒 `run`，当时经 `uv run` 启动），以及"`media_agent/` 与 `deploy/` 里没有任何锁"的核实 |
| §6 | 实测一轮的成本：170 部番、2,325 个文件、539 个种子，`diagnose` 约 44 秒，完整 `run` 约 1.5–2 分钟（运行锁等待时长据此取 10 秒） |
| §8a | 定点循环的设想里对"永远被跳过的动作"的统计：`rename_show_dir`「目标目录已存在」连续 53 轮、「集位被占」28 轮（到 2026-09-24）、`delete_category`「仍有种子」反复出现——每轮单独看都只是一条 skipped |
| §8c | 设想中的"每 30 分钟只抓取"模式：必须与 `run` 共用一把锁，否则落在 `run` 诊断与执行之间的抓取会被诊断期的 sidecar 快照盖掉，同一集再抓一遍 |

## deploy 调研

| 编号 | 是什么 |
|---|---|
| §3 | 生产上 launchd 怎么跑：`uv run` 不带 `--frozen`，每轮同步依赖；全项目没有任何锁 |
| §5 | 三处用 `Path(__file__).resolve()` 定位项目根，审计里存的是隔离区绝对路径：`releases/<sha>` + 软链的布局会让 `state/`、`.env`、规则、偏好"分家"，所以只能原地 checkout |

## grab 调研：抓取链路（第 2 阶段引用）

| 编号 | 是什么 |
|---|---|
| §2 | 调用关系核查：`grabber.add_and_name` 零调用方（本地两份克隆、tests / tools / deploy、生产机全部 `*.py` / `*.sh`），它的模块文档却自称"加种子的唯一入口"；真正的入口是 `QBitClient.add_torrent` |
| §4 | 抓取后即时改名撞上别人集位名的两种入口：下面的 S1（停滞放行换源）与 S3（与 AutoBangumi 赛跑）。`tests/test_grab_claims.py` 的模块文档引用它 |
| §5 | "两个种子一个路径"的闸门规格：盘上 `X` / `X.!qB` + qBittorrent 里别的种子的条目，NFC + casefold 比较，豁免问的人自己；接到抓取后改名、`_op_rename`、relink 等所有写路径。实现见 `architecture/2026-09-26-path-claims.md` |
| S1 | 停滞放行换源：旧种子停滞超过 `DEAD_TORRENT_HOURS`，抓取放行新源；抓取（op 0）早于死种摘除（op 1），新种子被即时改名到旧种子仍声明、盘上还有它 `X.!qB` 的集位名上 |
| S3 | 与 AutoBangumi 赛跑：诊断之后、加种之前 AB 下完并改名到 X；新种子被映射到 X，完成时 `X.!qB → X` 撞 EEXIST，偏好的版本（如邪竜解放版）作为孤儿 `.!qB` 静默留下 |

## purge / destructive 调研：隔离区的硬删除（第 2 阶段引用）

| 编号 | 是什么 | 处理 |
|---|---|---|
| purge 缺口 (a) | `build_pool` 的替代者按文件名 `SxxEyy` 正则认，不按判重的 `_resolve`；替代者"是这一集"没有独立确认（2026-08-31 Re:Zero：`3rd Season - 08` 被改名成 `S01E08`，它会替 2016 年的原片"作保"） | 963f2bc |
| purge 缺口 (b) | 替代者是不是更该留的那份不看：2026-09-08 穹庐下的魔女 S01E11 带两条中文内封字幕的那份输在体积上被隔离，当天人工捞回 | a9bed0c |
| purge 缺口 (c) | 原路径是否仍被活种子声明不看（`qbit.delete` 失败只记日志、照样搬走的那种）；`.!qB` 检查拿隔离区路径去比，永远通过 | 6e62bd8 |
| purge 缺口 (d) | 没有最短隔离期：一分钟前隔离的也能被 `purge --apply` 删掉，那一批回退失效 | 4f01ab8 |
| purge P5 | 时间清理并进统一的处置、每一次硬删除逐个文件留记录 | 273a072（预写日志）、8faf076（换掉 `rmtree`） |
| purge P7 | unlink 之前按此刻复核，并与 `run` 共用锁 | d123a61；锁见 0b5a02e |
| destructive I | `run` 末尾的时间清理：超过 30 天的日目录整个 `rmtree`、不写记录（生产 run.log 两次共 6 个文件 4.7 GB） | 8faf076 |
| destructive J | `purge --apply`：替代者按名字认、`.!qB` 检查形同虚设 | 963f2bc、6e62bd8 |

## 第 2 阶段审查（2026-09-26）：单点变异编号

第 2 阶段分支（46c48d5）的测试视角审查：208 个单点变异（`if 条件:` → `if False:`、删掉一个过滤、换成拷贝……），
53 个在全套测试下存活；另有 X01 这样的"可达探针"（把入口换成 `raise`，看有没有测试走到）。变异脚本不入库。
测试与提交说明里引用的编号如下，"处理"是让它被杀的提交。

| 编号 | 变异了什么 | 处理 |
|---|---|---|
| X01 / P32 / P33 / P34 | `purge._prove_duplicate` 无种子替代者的时长自证：入口换成 raise / 偏短、偏长、尾部解码三条判据各自关掉 | 31ff38c |
| G37 / G36 / G33 / G38 | `gate.other_holders`：不滤孤儿 `.!qB` / 不滤 0 字节 / 不滤特典名 / 不先认钉子 | 363441a |
| G17b | `gate._keeper_problem` 的 `fold(kp) == fold(path)` 改成逐字比较（只差大小写的保留方） | 99369f1 |
| G46 | `gate.describe`：集位不再优先取动作给的 `slot` | ebda404 |
| A13b | 逆改名盘上一侧（`if chk.claimants: 还原目标已存在`）关掉 | b1374a7 |
| A31 / A32 / A34 | 执行器：`_audit` 不作废占用索引 / 加种后不作废 / 索引拷一份 `_removed_torrents` 而不是按引用 | 837e5a7 |
| D09 / D10 / D14 / D15 / D16 / D16b | `naming.is_extra_of` 不剥剧名 / 剥剧名不看词边界；判重 / 改名 / 改名撞车 / 特典各自回到整名匹配 | 986ac45 |
| T02 / T08 | `_op_trash` / `_op_drop_torrent` 忽略关口的 `v.failed`（T02 早有测试） | T08：42188da |
| T05 | `_op_trash` 整种子作废时不记 `_removed_subjects` | ebda404 |
| A10 / A11 | relink 换目录时不查连带搬走的条目 / relink 回退不查盘上 | 0acb827 |
| A16 / A17 / A19 | 重加种子看不全照样加 / 没有候选路径照样加 / 忽略摘除时记下的 `paths` | 0acb827 |
| A26 / A27 | 目录改名：邻居 `files()` 读失败照样搬 / 回退时旧目录已在盘上照样搬 | 0acb827 |
| C18 / C19 | `claims` 读不了目录 / `lstat` 失败时当作不存在 | 0acb827 |
| P03 / P06 / P13 | 意图不 fsync / `dispose` 不调 `recover` / 没有 qBittorrent 时 `dispose` 不拒绝 | ebda404 |
| P22 / P51 / P53 | 记录没有原路径照删 / 不用 `deletion.slot` / 按文件名猜季号 | ebda404 |
