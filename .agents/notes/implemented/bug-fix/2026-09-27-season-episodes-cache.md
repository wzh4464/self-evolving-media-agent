# 抓取每一遍都不带缓存地问 TMDB 分集表：按季缓存，取不到的负缓存

**日期**: 2026-09-27
**状态**: implemented / bug-fix
**触发**: 整改第 4 阶段"一轮之内收敛"（`run` 迭代到不动点，最多 3 次）之前的成本核算。runloop 调研
（2026-09-26，生产 170 部番、129 个 sidecar、159 个季键）：一次 `diagnose` 43.98 秒，其中 episode-available
31.9 秒，第二遍热跑照样 32.0 秒；其余 46 条规则合计约 0.7 秒。

## 现象

- `EpisodeAvailableDetector` 对每个 sidecar 季调 `ctx.tmdb.season_episodes`，**不带缓存**。同一个 (tmdb_id, 季)
  incomplete-season / source-abandoned 早就走 `tmdbeps:` 缓存（6 小时），抓取没走。迭代到不动点时，每次迭代
  都要再付 30 多秒、159 次请求。
- 取不到的季每次都重新问：每个检测器、每一遍各问一次。TMDB 挂着时每个季键各等一次 20 秒超时。
- source-abandoned 取失败时把 `{"eps": []}` 当真数据写进缓存 6 小时。它在订阅检测器里排在 incomplete-season
  前面：之后 6 小时 incomplete-season 与 sidecar-sync 读到的都是"这一季一集都没有"。

## 做法

- **一个入口** `cache.season_episodes(ctx, cache, tmdb_id, 季) -> (分集表 | None, 为什么)`，三个取分集表的检测器
  （incomplete-season、source-abandoned ×2、episode-available）都走它；sidecar-sync 只读缓存
  （`Cache.get_episodes`），时效口径与取的一方相同。
- **时效按分集表自己定**（`cache.episodes_ttl`）：播完的季 7 天（`EPISODES_ENDED_TTL`）；有没播或没定档的集、
  最后一集播出不到 14 天（`EPISODES_ENDED_AFTER_DAYS`：TMDB 还没把后面的集登上去时按在播算）、还没有集的，
  6 小时（`EPISODES_TTL`，与以前一样）。
- **取不到的负缓存 6 小时**（`tmdbepsfail:<id>:<季>`，`EPISODES_FAIL_TTL`）；取到了就删掉负缓存。负缓存生效期间
  调用方**照样每一遍说一句**"这一季这一轮不评估"（悄悄停摆必须被看见），只是不再打网络。
- **TMDB 连不上**（超时、连不上、5xx、429）时，同一个 Context——也就是一轮 `run` 的全部迭代——里没缓存的季不再问
  （`ctx.tmdb_episodes_down`），与扫描那边"出过一次错其余不再打网络"同一个口径。TMDB 回 4xx（这一季在 TMDB 上
  不存在）只是这一个键的答案，别的季照问。
- 失败原因只留异常类型与 HTTP 状态码：httpx 的报错文本带着整个请求 URL（`api_key=` 就在里面），这句话要进缓存与日志。
- source-abandoned 取失败时照旧按"没有已播的集"往下走，但不再缓存 `[]`。

## 量了多少

离线基座按生产规模搭：129 部番、159 个季键（25 个在播），`FakeTMDB.season_episodes` 每次睡 0.05 秒
（生产约 0.2 秒一次：31.9 秒 / 159 次）。同一个脚本分别跑改之前（HEAD `9568ea7`）与改之后：

| | 第一遍 diagnose | 第二遍（= 下一次迭代 / 下一次 diagnose） |
|---|---|---|
| 改之前 | 18.31 秒，318 次请求 | 9.13 秒，159 次请求 |
| 改之后 | 9.33 秒，159 次请求 | 0.10 秒，0 次请求 |

换算到生产（0.2 秒一次）：改之前每轮 `run` 光分集表就是 159 次（抓取）+ 最多 159 次（incomplete-season 的 6 小时
缓存与 6 小时一轮的节奏几乎每轮都过期）≈ 32–64 秒；改之后每轮第一次迭代只问在播的季与 7 天到期的那一小部分
（假设 25 个在播 + 134/28 ≈ 5 个到期，约 30 次 ≈ 6 秒），**第二次迭代起 0 次**。部署后第一轮会把 159 个季键
全取一遍（与以前一轮的量相当）。

## 测试

`tests/test_episode_cache.py`：第二遍 diagnose 一次都不问（改之前红）；在播的季 7 小时后重取；播完的季 2 天后照用、
8 天后重取；`episodes_ttl` 的判据；sidecar-sync 读 2 天前的播完季不再把 total 拿掉；取不到的 6 小时内不再问、过了
再问，生效期间每一遍照样说；TMDB 连不上时同一个 Context 里只问一次；某一季 404 不拦别的季；source-abandoned 取失败
不再把 `[]` 写进缓存（改之前红）。`FakeTMDB` 的 404 带上 `response.status_code`，与真客户端的 `httpx.HTTPStatusError` 同形。
