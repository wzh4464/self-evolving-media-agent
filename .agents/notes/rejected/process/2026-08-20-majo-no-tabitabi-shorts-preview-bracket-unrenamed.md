# 为魔女之旅 .shorts 的 Preview 原始命名补一条窄规则

Status: rejected
Rule: `majo-no-tabitabi-shorts-preview-bracket-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

样本显示 12 个文件位于《魔女之旅》的 .shorts 目录，文件名形如 [Airota&VCB-Studio] Majo no Tabitabi [Preview03][Ma10p_1080p][x265_flac].mkv，未转换成 {official_title} SxxExx 规范命名；现有 preview_episode_unrenamed 规则均未覆盖这批文件。

## 决定

增加一条仅限定魔女之旅 + .shorts + 文件名含 [PreviewNN] + .mkv 的规则，将这些文件标记为已知的预览集未规范化问题，不附加动作，因为目前无法可靠推导 TMDB/季集号或目标文件名。

## 放弃的替代方案

考虑过直接 rename 或 relocate，但缺少准确的剧集映射和目标路径；也考虑过扩展现有 majo-no-tabitabi preview 规则，但现有规则已有多条且边界不清，新开窄规则更安全。

## 风险与缓解

可能命中 .shorts 中未来新增的同类 Preview 原始命名文件，这是预期行为；已规范文件不会再含 [PreviewNN]，因此误伤风险很低。

## 影子验证

```json
{
  "hits": 0,
  "covered_residue": false,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 12,
  "overlap_with": null,
  "verdict": "REJECT",
  "reject_reasons": [
    "没有覆盖它本该解决的样本",
    "一个都没命中"
  ]
}
```

## 规则定义

```json
{
  "id": "majo-no-tabitabi-shorts-preview-bracket-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取魔女之旅 .shorts 目录中仍带 [PreviewNN] 原始命名且未规范化的 .mkv 文件",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "official_title",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "ext",
        "op": "eq",
        "value": "mkv"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Preview\\d+\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
