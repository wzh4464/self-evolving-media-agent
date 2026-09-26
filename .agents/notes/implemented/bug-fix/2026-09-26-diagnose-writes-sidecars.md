# `diagnose` 改写了 sidecar：检测期只许读媒体根

**日期**: 2026-09-26
**状态**: implemented / bug-fix
**触发**: 整改第 4 阶段（身份与纯度）前的状态测绘：生产上同一天 12:01–12:02 有 5 份 `.media-agent.json`
被改写，而 `audit.jsonl` 的最后一条停在 11:17——是另一个进程在主机上跑检测器（`diagnose` / `apply --dry-run`）。

## 现象

抓取检测器 `EpisodeAvailableDetector` 为每部缺集的番找 Mikan 番组页（`grab._resolve_mikan_id`），
选中之后当场 `save_sidecar(show.dir_path, sc)` 把 `mikan_id` 写回 sidecar。这发生在**检测期**：

- `diagnose`、`apply --dry-run` 这些"只读"命令也改写媒体库里的文件；
- 演进器的影子验证会跑全部检测器，同样会写；
- 改动不经执行器：没有审计、没有逆操作，`rollback` 救不回来；
- 检测期写下的 sidecar 还会被同一轮 sidecar-sync 读到，决策与记账搅在一起。

## 做法

- 选中的番组页记在 `state/cache.sqlite3`（`mikanpick:<tmdb_id 或目录名>:<季>`，90 天），不写 sidecar。
  它只是下一轮候选里排第一的——每轮照样按播出日期重新打分，所以放在可丢的缓存里无妨。按季记：
  同一部番的不同季常在不同的番组页（入间同学的 2839 是第三季的页面）。
- sidecar 里的 `mikan_id` 从此**代码只读不写**：现存的值（历史上自动写的，或人填的）照旧当候选。
- 抓取的发现在 `evidence.mikan_id` 里写明用的是哪一页——以前能在 sidecar 里看到，现在在诊断输出与发现历史里看到。

检测期其余的写（`cache.put_*`：TMDB、feed、搜索、内容哈希）都落在 `state/`，不在媒体根下。全包 grep
（`write_text` / `.save(` / `rename` / `unlink` / `shutil` …）确认检测路径上只有这一处写媒体根。

## 测试

`tests/test_diagnose_purity.py`：一个会走到"选番组页"的抓取现场 + 一个待改名的种子 + 一份待同步的 sidecar，
跑真的 `cmd_diagnose` 与 `cmd_apply --dry-run`，媒体根下每个条目的类型、大小、mtime、小文件内容前后一致
（改之前两条都红）；搜索缓存过期、再搜搜不到时，记住的那一页仍被选中，sidecar 里的 `mikan_id` 仍为空。
