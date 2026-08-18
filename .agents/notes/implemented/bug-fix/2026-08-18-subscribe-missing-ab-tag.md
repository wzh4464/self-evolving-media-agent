# 新订阅补的历史集数拿不到 `ab:` 标签，永远不会被自动改名

Status: implemented
Class: bug-fix
Rule: `missing-ab-tag`

## 现象

通过 `/api/v1/rss/subscribe` 新订阅两部当季番（感谢对战。～大小姐才不玩格斗游戏～、
再见菈菈），接口两次都返回：

```json
{"msg_en": "[Engine] Download 再见菈菈 successfully.", "msg_zh": "下载 再见菈菈 成功。"}
```

qBit 里 13 个种子也确实落地了，分类 `Bangumi`、save_path 精确指向剧集目录，
看起来完全正常。但：

```
tags=''  [Nix-Raws] Sayonara Lara S01E01 [CR WEB-DL 1080p AVC AAC][SC_TC].mkv
tags=''  [Nix-Raws] Tai-Ari deshita Ojou-sama wa Kakutou Game nante Shinai S01E01 ...
```

**13 个种子全部没有 `ab:` 标签**，而库里既有的 370 个种子都有 `ab:N`。
AB 改名时靠这个标签反查 `episode_offset`，没有标签就永远不会被改名——
文件会一直停在字幕组的原始发布名上，Jellyfin/Infuse 刮削不到。

## 根因

`module/manager/collector.py::subscribe_season` 的执行顺序：

```python
data.added = True
data.eps_collect = True
await engine.add_rss(...)
result = await engine.download_bangumi(data)   # ← 先下载
engine.bangumi.add(data)                       # ← 后入库，此刻才分配 id
```

而打标签的代码在 `module/downloader/download_client.py::add_torrent`：

```python
tags = f"ab:{bangumi.id}" if bangumi.id else None
```

`download_bangumi` 跑的时候 `data` 还没进库，`data.id` 是 `None`，
于是 `tags = None`，qBit 收到的请求里根本没有 tags 字段。

这是 AutoBangumi 自身的顺序 bug，**影响每一次新订阅**：`eps_collect=True` 补下来的
整季历史集数全部无标签。之后定时 `refresh_rss` 新增的集数不受影响——那时
bangumi 已经有 id 了。所以症状表现为"新订阅的番，旧集永远不改名、新集正常改名"，
很容易被误判成个别集数的解析问题。

顺带一提：`torrent` 表里这 13 条记录的 `bangumi_id` 也是 `NULL`，同样源于这个顺序。
但那个不影响功能——去重走的是 URL，不是 bangumi_id（库里 579 条记录有 288 条
`bangumi_id` 为 NULL，属常态）。

## 修复

检测器 `missing-ab-tag`（`plugins/subscription.py`）。

判定条件是**双重确认**，缺一不可：

1. 种子的 `save_path` 与某条 bangumi 的 `save_path` 精确相等
2. 种子名能被该 bangumi 的匹配模式（`title_raw` + `title_aliases`）子串命中

只满足第 1 条不够。同一保存路径下若认错了番，而那条 bangumi 的 `episode_offset`
非 0，AB 会按错误的 offset 改名，把集数算错——**那比不改名更难收拾**，因为
文件名看起来是合法的，错误不会自己暴露。所以宁可保持沉默让人工介入。

命中即产出 `retag` action（`add_tags` 走 qBit API，符合"改动一律经 qBit"的约束，
且 executor 会记录 undo）。路径匹配不到任何订阅的种子不报——那多半是手动
添加的，报出来只是噪音。

## 验证

用真实数据的内存副本跑四个用例（不触碰线上状态）：

| 用例 | 期望 | 结果 |
|---|---|---|
| 剥掉 13 个种子的标签 | 全部检出，提议标签与原值一致 | 13/13 ✅ |
| 路径命中但种子名认不出 | 保持沉默 | 0 条 ✅ |
| 同样的种子但分类非 Bangumi | 跳过 | 0 条 ✅ |
| 线上真实状态 | 干净 | 0 条 ✅ |

## 教训

**AB 的成功消息不能当验证。** 这已经是第二次了——上一次是 `refresh_rss` 在
`add_torrent` 失败时仍无条件把 `downloaded` 置 True。这次是返回
"Download successfully" 但种子处于半残状态。凡是经 AB 的写操作，
一律回 qBittorrent 实地核对，而不是读它的返回值。
