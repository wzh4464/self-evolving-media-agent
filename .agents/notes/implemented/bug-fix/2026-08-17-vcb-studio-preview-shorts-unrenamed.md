# 补漏：Preview 特典未被现有 shorts 命名规则覆盖

Status: implemented
Rule: `vcb-studio-preview-shorts-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

观察到 [Airota&VCB-Studio] Majo no Tabitabi [PreviewNN] 系列文件存放在 .shorts 目录，文件名保留发布组原始命名（含 [Preview03]~[Preview11] 标记），未按 {official_title} SxxExx 规范重命名。现有规则 vcb-studio-sp-shorts-unrenamed 未命中这批文件，推测其匹配的是 SP 特典模式而非 Preview 模式。

## 决定

新增一条只针对 .shorts 目录中带 [PreviewNN] 标记且未重命名的文件的规则，标记为 minor 级别，不附加动作，仅让这批文件成为已知问题并供后续补全命名映射。

## 放弃的替代方案

考虑过扩展 vcb-studio-sp-shorts-unrenamed 的 match 条件来同时覆盖 Preview，但会破坏已有规则的职责边界且需要更复杂的 regex；也考虑过给 rename 动作，但 Preview 编号与正片集数的映射关系不明，无法安全生成新文件名，故放弃。

## 风险与缓解

误伤风险低，因为 [PreviewNN] 标记高度特化，且限定在 .shorts 目录。若未来有正常规范命名但文件名恰好含 [PreviewNN] 的文件，not_regex 条件可将其排除。

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
  "id": "vcb-studio-preview-shorts-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "VCB-Studio 发布的 Preview 特典视频未按规范命名",
  "match": {
    "all": [
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Preview\\d{2}\\]"
      },
      {
        "field": "filename",
        "op": "not_regex",
        "value": "^魔女之旅 S\\d{2}E\\d{2}"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
