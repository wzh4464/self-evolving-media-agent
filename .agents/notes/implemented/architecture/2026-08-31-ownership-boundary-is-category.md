# 所有权边界是 qBittorrent 的「分类」，不是 `ab:` 标签

## 结论先行

AutoBangumi 的改名线程扫的是：

```python
# module/downloader/download_client.py:125
async def get_torrent_info(self, category="Bangumi", status_filter="completed", tag=None)
```

`module/manager/renamer.py:446` 用默认参数调用它。**`tag=None`——标签从不参与筛选。**
AB 里所有 `get_torrent_info` 调用都按 `category` 过滤，没有一处按 tag。

所以：

| 分类 | 归谁 | 谁改名 |
|---|---|---|
| `Bangumi` | AutoBangumi | AB（`rename_time=60`，每分钟扫一次已完成的） |
| `<剧名>` | media-agent | media-agent（AB 查不到这个种子） |

`category-consolidation` 规则就是交接动作。实测：445 个带 `ab:` 标签的种子
**全部**已被改成剧名分类，AB 一个也扫不到，当前真实冲突 **0 个**。

## 之前写错的那条约束

AGENTS.md 原第 6 条说「手动往 qBittorrent 加种子会丢 `ab:` 标签、
永久脱离自动改名管辖」。**反了**：丢标签不影响 AB 改名，
改分类才影响。2026-08-31 试过摘掉 Re:Zero E58 的 `ab:9` 让 AB 别管，
14:42:03 它照样改了回去：

```
Re：从零开始的异世界生活 S01E58.mkv >> Re:从零开始的异世界生活 S01E08.mkv
```

`ab:` 是加种子时写下的**来源标记**，不是控制开关。

## AB 的改名结论由什么决定

```python
# renamer.py:459
bangumi_name, season = self._path_to_bangumi(save_path, torrent_name)
# torrent_parser.py:104
match_names = [torrent_name, media_path]     # 种子名优先，文件名只兜底
# renamer.py: gen_path(method="advance")
f"{bangumi_name} S{season:02d}E{episode + episode_offset:02d}{suffix}"
```

三个输入，全都在 media-agent 手上：

1. **种子名** —— 决定解析出的集号。`torrents/rename` 只改本地显示名，
   不动 infohash、不影响做种。要让 AB 算出别的答案，改这个。
2. **save_path** —— 决定 `bangumi_name` 和 `season`。
3. **`bangumi.episode_offset`** —— 单值偏移。

所以不必"屏蔽"AB，让它从同一个输入得出同一个答案即可。

## 今天那次误删的真正起点

不是 AB 越界，是 **media-agent 把自己下的种子放进了 AB 的地盘**：

```python
# actions.py _op_grab_episode（已修）
data = {"savepath": ..., "category": "Bangumi", ...}
```

写死的 `"Bangumi"` 等于主动交出改名权。AB 随即把
`[Fyy Raws] ... 3rd Season - 08` 改成 `S01E08`，撞上 2016 年真正的第 8 集，
判重按画质把 1.31GB 的原片清进隔离区。

改成 `category = show.official_title` 之后，自己抓的种子从一开始就在
自己的地盘，AB 全程看不见。

## 两道防线

1. **不进对方地盘**：grab 用剧名分类。
2. **不对未交接的文件做不可逆处置**：`duplicate-episode` 遇到
   `torrent_category == "Bangumi"` 的候选就整桶跳过，报 `pending_ownership`。
   还挂在 `Bangumi` 下的文件，此刻叫什么只是"AB 认为的"，不是定论。

## 残留风险

AB 的 `episode_offset` 是**每条订阅一个值**，表达不了「同一目录里不同来源季
用不同偏移」。目前只有 Re:Zero 同时满足「配了 `season_offsets`」和「有 AB 订阅」，
但它的订阅锁定 `subgroupid=554`（百冬练习组），该组用连续编号，
`episode_offset=0` 对它是正确的。

**风险在换源时**：若 `repoint_rss` 把订阅切到一个按分季编号的组，
`episode_offset=0` 会静默变错。换源后应复核这个字段。
