# 覆盖 Re:Zero S2 VCB 残留 NCED/SP 特典

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-residual`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

该簇 27 个文件均来自 [VCB-Studio] 的 Re:Zero 第二季，文件名含 NCEDxx_EPxx 或 SPxx_xx 标记，目录为 .other 或 .shorts，torrent 字段为空，说明是订阅/导入链路之外的残留。现有规则 rezero-s2-vcb-nced-sp-unrenamed 系列未能命中这批标记组合。

## 决定

加一条声明式规则，按 show_dir + 文件名前缀 + NCED/SP 标记的精确模式匹配，把这些文件标记为已知的 special_episode_unrenamed 问题，不附加动作。

## 放弃的替代方案

考虑过扩展现有 rezero-s2-vcb-nced-sp-unrenamed 规则，但其 match 条件无法确知且可能误伤已规范文件；也考虑过按 parent_dir 为 .other/.shorts 来抓，但太宽会命中其他节目。放弃。

## 风险与缓解

误伤风险低——show_dir 精确锁定节目，文件名 regex 要求以 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season 开头且紧跟 NCED/SP 标记。若未来出现同节目其他规范命名的 VCB 文件，不会命中此模式。

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
  "id": "rezero-s2-vcb-nced-sp-residual",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "命中 Re:Zero 第二季 [VCB-Studio] 残留的 NCED/SP 特典文件，标记为已知未规范命名问题",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED\\d+_EP\\d+|SP\\d+_\\d+)\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
