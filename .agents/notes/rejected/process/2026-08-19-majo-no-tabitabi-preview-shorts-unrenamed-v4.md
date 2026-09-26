# 魔女之旅 Preview 片段仍未命中既有规则，需继续收窄排查

Status: rejected
Rule: `majo-no-tabitabi-preview-shorts-unrenamed-v4`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅 .shorts 目录下仍有 12 个 [Airota&VCB-Studio] Majo no Tabitabi [PreviewNN] 命名的 mkv 文件，未按 {official_title} SxxExx 规范重命名。此前已有三条针对同问题的规则（unrenamed、v2、v3）及一条 untracked 规则，但均未覆盖这批样本。

## 决定

新增一条只匹配 show_dir=魔女之旅、parent_dir=.shorts、文件名含 [Airota&VCB-Studio] Majo no Tabitabi [PreviewNN] 模式且扩展名为 mkv 的规则，将这批残留文件标记为已知的 preview_episode_unrenamed 问题。

## 放弃的替代方案

考虑过复用或扩展现有 v3 规则，但无法查看其具体匹配条件，且历史多次迭代仍漏报，说明原规则存在未覆盖的边界；与其修改可能误伤其他文件的旧规则，不如新增一条窄规则精确兜底。

## 风险与缓解

若未来用户手动将同目录下其他 Airota&VCB-Studio 的 Preview 文件规范命名，本规则可能短暂误报，但该命名模式本身即违反规范，命中即问题，误伤风险可接受。

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
  "id": "majo-no-tabitabi-preview-shorts-unrenamed-v4",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "魔女之旅 .shorts 目录下未重命名的 VCB-Studio Preview 片段",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Airota&VCB-Studio\\] Majo no Tabitabi \\[Preview\\d{2}\\]"
      },
      {
        "field": "ext",
        "op": "eq",
        "value": "mkv"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
