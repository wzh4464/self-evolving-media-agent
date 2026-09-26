# 覆盖无 torrent 元数据的魔女之旅 Preview 片段

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-untracked`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅 .shorts 目录下存在 12 个 Preview 片段，命名保持发布组原始格式，且 torrent_name、torrent_tags、torrent_state 全部为空，说明这些文件不在订阅追踪范围内。现有多个 preview/special 未重命名规则均未命中，因为这些规则依赖 torrent 元数据或其他路径特征。

## 决定

新增一条窄规则，以 parent_dir=.shorts、official_title=魔女之旅、filename 含 [PreviewNN]、torrent_name 为空四个条件联合匹配，精确覆盖这批无 torrent 关联的未重命名 Preview 片段。不附带 action，仅将其标记为已知问题，供后续人工判断是否保留或规范化。

## 放弃的替代方案

考虑过放宽为通用的 .shorts 目录无 torrent 规则，但样本仅覆盖魔女之旅一部作品，放宽会误伤其他正常存放的短片内容；也考虑过直接给 rename 动作，但 Preview 片段缺少权威的 SxxExx 映射依据，贸然重命名可能造成编号冲突或语义错误，故放弃。

## 风险与缓解

若未来其他作品在 .shorts 目录出现类似无 torrent 的 Preview 片段，本规则不会误伤，因为 official_title 和 filename 模式双重限定在魔女之旅；风险在于该簇中若有个别文件后续被正确关联 torrent，规则将不再命中，属于可接受的漏报而非误报。

## 影子验证

```json
{
  "hits": 12,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 12,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "majo-no-tabitabi-preview-shorts-untracked",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "捕获魔女之旅 .shorts 目录下无 torrent 关联且未规范命名的 Preview 片段",
  "match": {
    "all": [
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "official_title",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Preview\\d{2}\\]"
      },
      {
        "field": "torrent_name",
        "op": "eq",
        "value": ""
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
