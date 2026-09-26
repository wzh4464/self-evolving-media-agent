# 收编 Re:从零开始的异世界生活 S1 SP 短篇未重命名文件

Status: implemented
Rule: `rezero-s1-sp-shorts-unrenamed-v2`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

观察到 25 个文件位于 Re:从零开始的异世界生活 的 .shorts 目录，文件名保留 VCB-Studio 原始命名格式（含 SP01/SP02 编号），未按库内 `{official_title} SxxExx.ext` 规范重命名。现有规则中已有的 rezero-s1-sp-shorts-unrenamed 等规则未命中这批文件，说明其匹配条件过窄或已失效。

## 决定

新增一条精确匹配该剧集 .shorts 目录下 VCB-Studio SP 文件命名模式的声明式规则，将这批文件标记为已知的 special_episode_unrenamed 问题。不附加 action，因为 SP 特典的规范命名需要人工确认对应的剧集编号或特典编号，无法可靠推算。

## 放弃的替代方案

考虑过直接复用或扩展现有 rezero-s1-sp-shorts-unrenamed 规则，但该规则具体匹配条件未知且未命中本批文件，扩宽可能误伤已规范文件；也考虑过给 rename 动作，但 SP 编号到 SxxExx 的映射无可靠依据，硬猜会产生错误命名。

## 风险与缓解

规则限定 show_dir 精确匹配 'Re:从零开始的异世界生活' 且 parent_dir 为 '.shorts'，加上文件名正则严格限定 VCB-Studio SP 命名模式，误伤已规范文件的风险极低。若未来该剧新增其他压制组的 SP 文件，需另立规则覆盖。

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
  "id": "rezero-s1-sp-shorts-unrenamed-v2",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "识别 Re:从零开始的异世界生活 第一季 .shorts 目录下未按规范重命名的 SP 特典文件",
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
