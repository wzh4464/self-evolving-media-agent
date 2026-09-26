# 抓完不改名：所有权交接带来的六小时空窗

## 现象

用户报「最新的三集（20世纪、幼女战记、Kimi ga Shinu made Koi）刮削失败」。
实际是五集全部没改名，躺在库里还叫着原始发布名：

```
[Nekomoe kissaten][20 Seiki Denki Mokuroku][09][1080p][JPSC].mp4
[TSDM] Kimi ga Shinu made Koi wo Shitai [09][WebRip][HEVC-10bit 1080p AAC][CHS...
```

Jellyfin 认不出 `{标题} SxxExx` 以外的形态，于是刮削失败。

## 根因：是同一天早些时候的改动带来的回归

上午把 `_op_grab_episode` 的分类从写死的 `"Bangumi"` 改成剧名分类，
理由是正当的——AutoBangumi 只扫 `category="Bangumi"`，放进它的地盘就等于
交出改名权，而它会按发布方的分季集号改名，撞掉真实集数（Re:Zero E58 事故）。

但交接之后改名责任全归本项目，而本项目一轮 run 是**先全量诊断、再统一执行**：
抓取发生在执行阶段，`unrenamed-file` 的检测早就跑完了。新抓的集要等
**下一轮**（6 小时后）才改名。

以前落在 `Bangumi` 分类时，AB 的改名线程 60 秒就处理了。换成自己管之后
反而慢了六小时——正确性换来了延迟，而延迟这一头没人补。

## 修法

加种成功就立刻改名。集号在那一刻是确定的——就是钉进 `ma:` 标签的那个，
不需要再解析文件名：

```python
self._rename_grabbed(blob, official_title, season, ep)
```

只处理「恰好一个视频文件」的种子；合集或带特典的交给 `unrenamed-file`
逐个文件判断，这里不猜。改名失败只记日志不算抓取失败——下一轮的改名规则兜底。

`_infohash_v1` 拿来定位刚加进去的种子（409 重复时按名字反查不到），
qBittorrent 解析元数据要一点时间，所以轮询最多 10 秒等文件列表出现（2026-09-26 起改为可配，见文末「后续」）。

## 教训

**换掉一个组件时，要连它顺带提供的东西一起接管。** AB 不只是"会改错名"，
它同时也提供了"60 秒内改名"。只看到前者、把它请出去，就留下了后者的空缺。

## 测试踩的坑

第一次往返测试我挑错了种子——把真实的 E07 文件改成了假名字，
而且假名字的扩展名（`.mp4`）和真实文件（`.mkv`）不一致，
导致 `_rename_grabbed` 算出的目标名对不上，测试失败。

看着像函数有 bug，其实是测试脚本自己造的。已恢复 E07，
换成扩展名一致的对象重测，往返通过。

## 后续（2026-09-26，整改第 3 阶段）：等多久可配，等的结局进审计

- 等元数据的时长改成配置 `GRAB_METADATA_TIMEOUT`（秒，默认 30）。`grabber.wait_metadata` 的默认值与模块文档
  一直是 30 秒（磁力 / 连不上 peer 时元数据可能要几分钟），`_rename_grabbed` 却写死了 10——两处说法不一。
  写错（非数字、负数、nan、空）启动即报错，不静默退回默认值。
- 每次抓取的审计多一个 `metadata`：`outcome`（`ready` / `timeout` / `not_attempted`——纯 v2 种子算不出
  infohash）、`timeout_s`、`waited_s`，读 `files()` 报过错的再加 `last_error` 与 `errors`（次数）。以前只有
  `rename.skipped` 里一句「元数据 10 秒内未到」，分不清是真没元数据还是 qBittorrent 一直报错。
- 代价：元数据真的迟迟不来时，一次抓取最多多等 20 秒。.torrent 文件加种（本项目抓取的方式）元数据是现成的，
  正常情况下 `waited_s` 为 0。

测试：`tests/test_grab_metadata_timeout.py`。

