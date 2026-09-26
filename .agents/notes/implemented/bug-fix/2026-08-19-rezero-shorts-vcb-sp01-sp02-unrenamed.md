# 覆盖 Re:从零开始 .shorts 下 VCB-Studio SP01/SP02 未重命名文件

Status: implemented
Rule: `rezero-shorts-vcb-sp01-sp02-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

媒体库中 Re:从零开始的异世界生活 的 .shorts 目录下存在 25 个 VCB-Studio 压制特典文件，文件名保持发布原样（如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv），未按 {official_title} SxxExx.ext 规范重命名。现有多个 rezero SP unrenamed 规则均未命中这批文件。

## 决定

新增一条窄规则，精确匹配 parent_dir 为 .shorts、show_dir 为 Re:从零开始的异世界生活、文件名符合 VCB-Studio SP01/SP02 编号格式的 .mkv 文件，将其标记为 special_episode_unrenamed。暂不附加 action，因为无法从现有字段可靠推断正确的 SxxExx 编号映射（SP01/SP02 到季集数的对应关系不明确）。

## 放弃的替代方案

考虑过合并进现有 rezero-shorts-sp-unrenamed 或 rezero-s1-sp-shorts-unrenamed 规则，但经样本核对，现有规则匹配条件未能覆盖这批文件的文件名模式；若修改旧规则，可能误伤其他已规范的变体，故选择新增独立规则。也考虑过附加 rename 动作，但 SP 编号与目标集数的映射需人工确认，硬编码可能产生错误命名。

## 风险与缓解

误伤风险低，因为匹配条件同时约束了目录层级（.shorts）、剧名（Re:从零开始的异世界生活）和文件名正则（仅 SP01/SP02 且带 Ma10p_1080p 与 x265_flac）。若未来同目录出现已正确重命名的文件，其文件名不会匹配该正则，因此不会被误命中。

## 影子验证

```json
{
  "hits": 25,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 25,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "rezero-shorts-vcb-sp01-sp02-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:从零开始的异世界生活 .shorts 目录下 VCB-Studio SP01/SP02 特典文件未按规范重命名",
  "match": {
    "all": [
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP0[12]_\\d{2}\\]\\[Ma10p_1080p\\]\\[x265_flac\\]\\.mkv$"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
