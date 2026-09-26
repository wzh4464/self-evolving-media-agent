# Re:Zero S2 的 VCB-Studio NCED/SP 残留文件缺乏归属规则

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-leftover`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

该簇共27个文件，样本显示文件名均以 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season 开头，后接 [NCED02_EP45]、[NCED02_EP47]、[SP03_04]、[SP03_07]、[SP03_09]、[SP05_01] 等标记，散落在 .other 与 .shorts 目录。现有规则中 rezero-s2-vcb-nced-sp-residual、rezero-s2-vcb-nced-sp-unrenamed-v2 等名称相近，但显然其 match 条件未覆盖这些编号组合。

## 决定

新增一条 narrow 规则，用正则锚定 [VCB-Studio] Re Zero ... 2nd Season 开头且含 NCED 或 SP 数字编号的文件，并限定 show_dir 与 season_dir 以排除正片与已规范文件。不附加 action，因为这些特殊片段的正确命名需要人工确认 TMDB 条目。

## 放弃的替代方案

考虑过逐个扩展已有 rezero-s2-vcb-nced-sp-* 规则，但无法确定各规则原本的精确编号范围，盲改有放宽已有规则的风险。也考虑过按 season_dir 单独为 .other 和 .shorts 各立一规，但两个目录中的文件名模式完全一致，合并为一条更简洁。

## 风险与缓解

误伤风险低，因为正则同时锚定了 [VCB-Studio] 前缀、Re Zero ... 2nd Season 标题、NCED/SP 编号，且限定 show_dir 与 season_dir。若未来这些文件被正确重命名，规则自然不再命中。

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
  "id": "rezero-s2-vcb-nced-sp-leftover",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "识别 [VCB-Studio] Re:从零开始的异世界生活 第二季中未被现有规则覆盖的 NCED/SP 系列残留文件",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP)\\d+"
      },
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "season_dir",
        "op": "in",
        "value": ".other|.shorts"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
