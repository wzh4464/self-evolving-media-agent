# VCB-Studio 预览特典文件未被现有 unrenamed 规则覆盖

Status: rejected
Rule: `majo-tabitabi-vcb-preview-shorts-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅 .shorts 目录下有12个以 [Airota&VCB-Studio] Majo no Tabitabi [PreviewNN] 命名的 mkv 文件，命名未遵循 {official_title} SxxExx 规范。现有大量 unrenamed 类规则（如 vcb-studio-preview-shorts-unrenamed、majo-no-tabitabi-preview-shorts-unrenamed 及其 v2/v3 变体）均未命中这批文件，说明此前针对该剧集和该制作组的规则匹配条件与实际文件名存在偏差。

## 决定

新增一条窄规则，精确锚定 show_dir=魔女之旅、parent_dir=.shorts、文件名含 [Airota&VCB-Studio] Majo no Tabitabi [PreviewNN] 且扩展名为 mkv 的文件，将其归类为 preview_episode_unrenamed，使这批文件作为已知问题被看见。不附加 action，因为无法可靠推断每个 Preview 编号对应的目标 SxxExx 集数。

## 放弃的替代方案

考虑过放宽到所有 VCB-Studio Preview 文件（去掉 show_dir 锚定），但那样可能误伤其他剧集中已正确处理或命名规范不同的文件。也考虑过复用现有 vcb-studio-preview-shorts-unrenamed 规则，但其匹配条件未能命中本批文件，直接修改现有规则影响面不可控，故选择新增窄规则。

## 风险与缓解

误伤风险低：锚定了具体剧集目录、.shorts 父目录、具体命名前缀和 mkv 扩展名，命中范围极窄。若未来这些文件被正确重命名，规则自然不再命中。

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
  "id": "majo-tabitabi-vcb-preview-shorts-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "魔女之旅 .shorts 目录下 VCB-Studio 的 Preview 预览特典文件未按规范重命名",
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
