# 补充 Re:Zero S2 VCB-Studio NCED/SP 未改名残留规则

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-unrenamed-residual`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

这批文件仍以 [NCED02_EP45]、[SP05_01]、[SP03_04] 等 VCB-Studio 原始括号命名存在，位于 .other/.shorts 下，现有多个 rezero s2 vcb nced/sp 规则没有命中。

## 决定

新增一条仅报告问题的窄规则，用 show_dir、season_dir 和 VCB-Studio 2nd Season 文件名正则组合锁定这批残留文件，不执行动作，因为无法可靠地把 NCED/SP 编号映射为 SxxExx。

## 放弃的替代方案

考虑过直接扩展现有 rezero 规则，但会提高误伤已规范文件的风险；也考虑过 rename 或 relocate，但缺少可靠的 TMDB/剧集映射和目标目录信息，故放弃动作。

## 风险与缓解

可能误伤该目录下已被人工改名但仍保留 VCB-Studio 2nd Season 前缀的文件；通过 season_dir 限定在 .other/.shorts 以及仅报告不动作来降低风险。

## 影子验证

```json
{
  "hits": 27,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 27,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "rezero-s2-vcb-nced-sp-unrenamed-residual",
  "kind": "special_episode_unrenamed",
  "severity": "important",
  "summary": "识别 Re:Zero 2nd Season 在 .other/.shorts 中仍保持 VCB-Studio NCED/SP 原始命名的未改名文件",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "season_dir",
        "op": "in",
        "value": [
          ".other",
          ".shorts"
        ]
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP)[0-9]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
