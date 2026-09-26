# 覆盖 NUKITASHI 未重命名字幕

Status: implemented
Rule: `nukitashi-s1-sup-unrenamed`
Kind: `subtitle_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《住在拔作岛上的我应该如何是好？》Season 1 下存在 6 个 .sup 字幕文件，文件名仍为【7月】NUKITASHI...【全面限制 ver./配信限定 ver./青蓝岛 ver.】.中文（简体/繁體）的原始发布样式，未匹配任何现有 unrenamed 规则。

## 决定

新增一条窄规则，仅匹配该剧 show_dir 下文件名含 NUKITASHI 发布前缀的 .sup 文件，将其标记为已归类的已知未重命名问题，不自动执行动作。

## 放弃的替代方案

考虑过建立通用 .sup 未重命名规则，但其他剧字幕命名差异较大，容易误伤规范命名或尚未评估的文件；也考虑过按版本和简繁自动 rename，但缺少目标命名规范，生成占位名会被驳回。

## 风险与缓解

若未来该剧其他正常文件也包含【7月】NUKITASHI 前缀可能被命中，但正常文件应已按 official_title 重命名，风险很低。

## 影子验证

```json
{
  "hits": 6,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 6,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "nukitashi-s1-sup-unrenamed",
  "kind": "subtitle_unrenamed",
  "severity": "minor",
  "summary": "识别《住在拔作岛上的我应该如何是好？》中未规范命名的 NUKITASHI .sup 字幕文件",
  "match": {
    "all": [
      {
        "field": "ext",
        "op": "eq",
        "value": ".sup"
      },
      {
        "field": "show_dir",
        "op": "eq",
        "value": "住在拔作岛上的我应该如何是好？"
      },
      {
        "field": "filename",
        "op": "contains",
        "value": "【7月】NUKITASHI住在拔作岛上的我该如何是好？"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true,
  "resolution": "classified"
}
```
