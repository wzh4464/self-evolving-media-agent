# 番组 feed 缓存永不过期，抓取器三天看不见任何新发布

## 现象

用户问「怎么 Re:Zero 还没更新」。E81 于 2026-09-02 播出并发布，
9-03 库里没有。media-agent 每 6 小时正常跑，但**自 8-31 起零次 `grab_episode`**。

逐层排查，每一道闸都是通的：

```
1) tmdb 分组 kind = single          （不是重复目录）
2) sidecar.seasons keys = ['1','0']
3) 季号布局: 库内 [1] vs TMDB [1] -> 多余 []
4) inflight = {}
5) S1: eps=85 seasonal=True have=80 aired=81 missing=[81]
```

`missing=[81]`，候选也算得出来：

```
_episode_of      = 15          （番组页按分季编号）
declared_season  = 4
season_offsets   = {'4': 66}
换算后集号       = 81
pubDate          = 2026-09-02
```

可检测器输出 **0 条**。

## 根因

`_feed_cached` 用 `Cache.get_llm`，而那张表**没有过期检查**：

```python
got = cache.get_llm(ck)
if got is None:              # 只有"从没缓存过"才去拉
    got = {"items": _feed_items(mid)}
    cache.put_llm(ck, got)
```

于是每部番的番组页在第一次抓取后**永久冻结**。实测：

| | 条数 | 最新发布日 |
|---|---|---|
| 缓存中的 3951（写于 71 小时前） | 180 | 2026-08-26 |
| 实时拉取 | 181 | **2026-09-02** |

快照定格在 8-31 抓 E80 的那一刻。库里有 **60 个**这样的冻结快照——
「谁先出要谁」的模型从原理上再也看不见任何新发布，而且**不报任何错**。

## 为什么昨天修 `tmdbeps` 时漏了它

昨天发现 `tmdbeps:` 存在无 TTL 的 `llm` 表里，把那一类挪去了带 TTL 的
`tmdb` 表。但只改了 `tmdbeps`，没问「这张表里还装着什么」。

审计发现：`llm` 表里**一条模型判断都没有**，装的全是网络数据，全都有时效性：

| 前缀 | 条数 | 最老 |
|---|---|---|
| `mikanfeed` | 60 | 318 小时 |
| `mikansearch` | 60 | 386 小时 |
| `mikansub` | 12 | 386 小时 |
| `rss` | 45 | 402 小时 |

表名 `llm` 是个误导——它是通用网络缓存，而"通用网络缓存没有 TTL"本身就是设计缺陷。

## 修法：让忘记变成不可能

`Cache.get_llm(key, ttl)` 的 `ttl` 改成**必填**，不给默认值。
漏掉任何一处调用会立刻抛 `TypeError`，而不是静默地把数据冻住。

TTL 按数据性质分两档：

- `FEED_TTL = 1h` —— 番组 feed、RSS 标题。这是"有没有新集"的唯一信号，
  必须远短于 6 小时的调度间隔。
- `LOOKUP_TTL = 7d` —— 标题→番组 id、字幕组列表。映射关系，稳定但不是永恒。

同时删掉 `media_agent/subscription.py`（全仓无引用的死代码，且带着同一个 bug——
留着只会让下次修复改错文件）。

## 解冻后暴露的规模

问题数 15 → 42。冻结的 feed 一直在掩盖全库的缺集：

| | 之前 | 之后 |
|---|---|---|
| `episode_grabbable` | 4 | 11 |
| `incomplete_season` | 1 | 6 |
| `sidecar_stale` | 1 | 16 |

## 顺带修掉的第二个：`have` 读的是滞后一轮的镜像

解冻后待抓列表里有《无职转生》S03E10——但它十六小时前就由 AutoBangumi
下完躺在磁盘上了，sidecar 的 `have` 仍停在 9。

一轮 run 是"先全量诊断、再统一执行"，抓取器读到的 sidecar 是**上一轮**写的。
AB 在两轮之间下完的集，磁盘上有、sidecar 里没有，于是被判成缺失并重下。

改成 `have = sidecar.have | 磁盘实况`。

这跟 8-31 那次 `seasonal` 标记是同一类问题：**依赖会滞后一整轮的持久化状态**。
凡是磁盘或 API 能直接回答的，就别问镜像。
