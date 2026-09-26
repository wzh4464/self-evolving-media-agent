# 新增 Re:Zero VCB SP01/SP02 双数字后缀短片识别规则

Status: implemented
Rule: `rezero-vcb-shorts-sp01-sp02-numeric-suffix`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

媒体库中存在25个文件，位于 Re:从零开始的异世界生活/.shorts/ 目录，文件名为 [VCB-Studio] 前缀且包含 [SP01_xx] 或 [SP02_xx] 格式的集数标记（如 SP01_07、SP02_14），不符合 {official_title} SxxExx.ext 的命名规范。现有规则中虽有多个针对 rezero SP 的规则（如 rezero-shorts-vcb-sp01-sp02-unrenamed、rezero-s1-sp-shorts-unrenamed-v2 等），但均未命中这批文件，说明它们的匹配条件与本批文件的实际命名模式存在差异。

## 决定

新增一条窄规则，仅匹配 parent_dir 为 .shorts、official_title 为 Re:从零开始的异世界生活、文件名匹配 [VCB-Studio] 前缀且含 [SP01_xx] 或 [SP02_xx] 双数字后缀的文件。规则只报告问题、不附加 action，因为无法从文件名可靠推断出正确的 SxxExx 编号（SP 编号与正片季集编号的映射关系不明确，需要人工确认）。

## 放弃的替代方案

考虑过复用或扩展现有 rezero-shorts-vcb-sp01-sp02-unrenamed 规则，但其匹配条件未知且未命中本批文件，扩展可能误伤已规范的 SP 文件。也考虑过按 size 范围（20-25MB）辅助匹配，但 size 无法可靠区分 SP 短片与其他内容，放弃。

## 风险与缓解

风险较低。规则限定在 parent_dir 为 .shorts 且 official_title 为 Re:从零开始的异世界生活 的范围内，且文件名必须含 [VCB-Studio] 前缀和 [SP01_xx]/[SP02_xx] 双数字后缀。若未来有文件已按 SxxExx 规范重命名，则不会命中此规则（因为不再含 [SPxx_yy] 格式标记）。正则中 \d{2} 要求恰好两位数字，与观察到的 SP01_04、SP01_07、SP01_09、SP01_11、SP02_01、SP02_14 等样本一致，误伤面极窄。

## 影子验证

```json
{
  "hits": 27,
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
  "id": "rezero-vcb-shorts-sp01-sp02-numeric-suffix",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "标记 Re:Zero 的 .shorts 目录下使用 [SPxx_yy] 双数字后缀命名、未按 SxxExx 规范重命名的 VCB-Studio 特典短片",
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
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[VCB-Studio\\].*\\[SP0[12]_\\d{2}\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
