# 补充 Re:Zero .shorts 目录 SP01/SP02 特殊篇的未重命名检测

Status: rejected
Rule: `rezero-sp01-sp02-shorts-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

Re:从零开始的异世界生活的 .shorts 目录下有 25 个文件（如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv）未被任何现有 special_episode_unrenamed 规则命中。现有规则覆盖了 S2 的 SP、NCED、预览短片等，但 SP01/SP02 编号的特殊篇（可能是 S1 的 Mini Anime 或类似内容）落在规则盲区。

## 决定

新增一条规则，精确匹配 show_dir=Re:从零开始的异世界生活 且 season_dir=.shorts 且文件名包含 [SP01_xx] 或 [SP02_xx] 模式的 mkv 文件，将其标记为 special_episode_unrenamed 问题。不给 action，因为无法确定正确的 SxxExx 编号映射。

## 放弃的替代方案

考虑过放宽到所有 .shorts 目录下的 VCB-Studio 文件，但会误伤已规范命名的文件；也考虑过合并到现有 rezero-s1-sp-shorts-unrenamed 规则，但该规则显然已有不同的匹配条件且未命中这批文件，修改现有规则风险更高。

## 风险与缓解

如果 .shorts 目录下有已规范命名的文件恰好也含 [SP01_xx] 模式（不太可能，因为规范命名不含方括号编号），可能误伤。但该模式明确指示 VCB-Studio 原始命名，规范命名不会保留此模式。风险极低。

## 影子验证

```json
{
  "hits": 0,
  "covered_residue": false,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 25,
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
  "id": "rezero-sp01-sp02-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:从零开始的异世界生活 .shorts 目录下含 SP01/SP02 编号的未规范命名特殊篇",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
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
        "value": "\\[SP0[12]_\\d{2}\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
