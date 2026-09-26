# Re:Zero S2 VCB NCED/SP 特典仍未纳入命名规范

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

27 个文件位于 Re:从零开始的异世界生活 的 .other 和 .shorts 目录，文件名均为 [VCB-Studio] 原始发布格式，包含 NCED02_EP45、SP03_04 等特典标记，未被任何现有 unrenamed 规则捕获。

## 决定

新增一条只匹配 Re:Zero 第二季 VCB-Studio 的 NCED 与 SP 文件、且位于 .other 或 .shorts 的规则，将这些文件标记为已知的特典未重命名问题，便于后续批量处理。

## 放弃的替代方案

考虑过扩展现有 rezero-s2-vcb-specials-unrenamed 规则，但该规则的具体匹配条件未知，贸然修改可能引入误伤；也考虑过给 action 做 rename，但 NCED 与 SP 特典的目标命名规范不明确，且特典通常不按 SxxExx 编号，贸然 rename 反而会破坏文件。

## 风险与缓解

误伤风险低：正则要求同时出现 [VCB-Studio] 前缀、2nd Season、NCED 或 SP 标记，且限定在 .other/.shorts 目录、剧名精确匹配。若未来同一目录出现已规范命名的 VCB 文件（不太可能保留原始前缀），本规则不会命中，因为规范命名不含 [VCB-Studio] 前缀。

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
  "id": "rezero-s2-vcb-nced-sp-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:Zero 第二季 VCB-Studio 的 NCED 与 SP 特典未按库规范重命名",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP)\\d+"
      },
      {
        "field": "season_dir",
        "op": "in",
        "value": ".other,.shorts"
      },
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
