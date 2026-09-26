# 补漏 Re:Zero 第二季 VCB-Studio 特典未改名文件

Status: implemented
Rule: `rezero-s2-vcb-nced-sp03-sp05-final`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

现有 36 条规则中已有大量针对 rezero special_episode_unrenamed 的规则，但这 27 个文件（NCED02_EP45/EP47 在 .other，SP03/SP05 系列在 .shorts）仍未被命中。它们与已覆盖文件同源（VCB-Studio Re:Zero S2），只是括号内的标识符组合未被现有规则匹配。

## 决定

新增一条窄规则，限定 show_dir 和 season_dir，用正则精确匹配 [VCB-Studio] Re Zero 2nd Season 后跟 NCEDxx_EPxx 或 SPxx_xx 的原始命名，仅报告问题不执行动作。

## 放弃的替代方案

考虑过合并到某条现有 rezero 规则中，但现有规则各自匹配的括号内模式差异较大，合并可能扩大误伤面。也考虑过按文件大小或 parent_dir 匹配，但前者不稳定、后者与 season_dir 冗余。

## 风险与缓解

正则锚定 [VCB-Studio] Re Zero 2nd Season 前缀，且限定在 .other/.shorts 目录，误伤已规范文件的风险极低。唯一风险是未来新加入同模式文件也会被报告，但这正是规则意图。

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
  "id": "rezero-s2-vcb-nced-sp03-sp05-final",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取 Re:Zero 第二季 .other/.shorts 目录中残留的 VCB-Studio NCED/SP 原始命名文件",
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
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED\\d+_EP\\d+|SP\\d+_\\d+)\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
