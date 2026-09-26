# 补全 Preview 预览集无 torrent 关联的识别盲区

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-untracked-v4`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅 .shorts 目录下存在 12 个 Preview 预览集文件（如 [Airota&VCB-Studio] Majo no Tabitabi [Preview03][Ma10p_1080p][x265_flac].mkv），命名未规范化且无任何 torrent 关联（torrent_name/torrent_tags/torrent_state 均为空）。现有 preview_episode_unrenamed 相关规则均未命中它们。

## 决定

新增一条窄规则，以 filename 匹配 Preview 编号模式、parent_dir 限定 .shorts、torrent_name 为空三个条件同时成立来捕获此簇，明确标记为 preview_episode_unrenamed 类型的已知问题。不附加动作，仅做识别与报告。

## 放弃的替代方案

考虑过直接给 rename 动作将其规范化为 SxxExx 格式，但 Preview 预览集通常不属于正片剧集序列，强制重命名反而可能破坏其原有语义；也考虑过用 trash 清理，但这些文件可能是用户有意保留的花絮内容，删除过于激进，因此放弃。

## 风险与缓解

误伤风险低：三个条件同时成立具有高度特异性。唯一潜在风险是未来用户自行规范命名后，该规则仍会命中无 torrent 的 .shorts 目录下 Preview 文件，但这正是规则的目标——识别未规范化的已知问题，而非立即执行破坏性操作。

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
  "id": "majo-no-tabitabi-preview-shorts-untracked-v4",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "捕获魔女之旅 .shorts 目录下所有无 torrent 关联的 Preview 预览集文件",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Preview\\d{2}\\]"
      },
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
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
