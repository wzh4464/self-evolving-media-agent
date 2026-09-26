# 补齐魔女之旅预览命名残留规则

Status: implemented
Rule: `majo-no-tabitabi-preview-bracket-group-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

现有 majo-no-tabitabi preview 系列规则未命中这批文件；样本均以 [Airota&VCB-Studio] 开头并含 [Preview03/05/06/08/10/11] 编号，位于 .shorts，仍保留发布组命名。

## 决定

新增只读规则，用精确发布组与 Preview 编号模式限定魔女之旅 .shorts，不附加动作，先将文件暴露为已知问题。

## 放弃的替代方案

考虑过泛化现有 majo preview 规则以覆盖，但会扩大命中面；考虑过 rename/trash，但无法可靠推断 SxxExx 或是否应保留，故省略 action。

## 风险与缓解

误伤风险很低，已限定 show_dir、season_dir、official_title 与完整文件名模式；若未来同名发布组的刻意保留文件仍可能命中，但符合未重命名簇。

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
  "id": "majo-no-tabitabi-preview-bracket-group-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "命中魔女之旅 .shorts 中带 [Airota&VCB-Studio] 与 [PreviewNN] 标记但未按规范重命名的预览视频",
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
        "field": "official_title",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[Airota&VCB-Studio\\] Majo no Tabitabi \\[Preview(0[1-9]|1[0-2])\\]\\[Ma10p_1080p\\]\\[x265_flac\\]\\.mkv$"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
