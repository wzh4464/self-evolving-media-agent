# 为未命中的魔女之旅 Preview shorts 新增窄规则

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-unrenamed-v3`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《魔女之旅》 .shorts 目录下存在多个文件名含 [PreviewXX] 的文件，如 [Airota&VCB-Studio] Majo no Tabitabi [Preview03][Ma10p_1080p][x265_flac].mkv，不符合 {official_title} SxxExx.ext 规范，且现有规则列表中的 majo-no-tabitabi-preview-shorts 相关规则均未命中。

## 决定

新增一条只匹配 show_dir=魔女之旅、season_dir=.shorts、文件名含 [Preview数字] 的规则，只报告问题，不执行动作，避免硬凑参数。

## 放弃的替代方案

考虑过修改现有 majo-no-tabitabi-preview-shorts-unrenamed-v2 规则以放宽条件，但可能影响其他已命中文件，故放弃，改为新增窄规则。

## 风险与缓解

误伤风险低：已规范命名的文件不应再保留 [PreviewXX] 形式，且受 show_dir 和 season_dir 双重限制。若未来有合法文件刻意保留该命名，可能被误标记，但概率极小。

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
  "id": "majo-no-tabitabi-preview-shorts-unrenamed-v3",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "捕获《魔女之旅》.shorts 目录下文件名含 [PreviewXX] 且未按规范重命名的预览短片",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
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
