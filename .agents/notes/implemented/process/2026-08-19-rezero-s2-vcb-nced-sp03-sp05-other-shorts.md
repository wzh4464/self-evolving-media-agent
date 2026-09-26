# 补充 Re:Zero S2 VCB 特典在 .other/.shorts 目录的未重命名规则

Status: implemented
Rule: `rezero-s2-vcb-nced-sp03-sp05-other-shorts`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

Re:从零开始的异世界生活第二季 VCB-Studio 片源中，27 个特典文件（含 NCED02_EP45/EP47、SP03、SP05 系列）未被重命名为规范命名，且分布在 .other 和 .shorts 目录。现有规则 rezero-s2-vcb-nced-sp-* 和 rezero-shorts-* 均未命中这批文件，推测是 match 条件的目录或命名模式与这批样本有差异。

## 决定

新增一条窄规则，只匹配该剧集目录下文件名含 VCB-Studio 且带 NCED/SP03/SP05 标记、season_dir 为 .other 或 .shorts 的文件。不附加 action，因为无法可靠推导出特典的 TMDB 编号或规范命名，仅作为已知问题暴露。

## 放弃的替代方案

考虑过合并进现有 rezero-s2-vcb-nced-sp-leftover 或 rezero-shorts-sp-unrenamed 规则，但未看到这些规则的 match 定义，无法确认差异根源，贸然修改现有规则可能影响已命中的正常行为，故选择新增独立窄规则。也考虑过用 parent_dir 替代 season_dir，但样本中两者值一致，保留 season_dir 更贴近目录语义。

## 风险与缓解

误伤风险低：match 同时限定 show_dir、文件名前缀和目录，三重条件足够窄。若未来这些文件被正确重命名或移动目录，规则将自动失效。未附加 action，不存在误删或误改风险。

## 影子验证

```json
{
  "hits": 16,
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
  "id": "rezero-s2-vcb-nced-sp03-sp05-other-shorts",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取 Re:从零开始异世界生活第二季 VCB-Studio 片源中散落在 .other/.shorts 目录、命名含 NCED 或 SP03/SP05 的未重命名特典文件",
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
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP03|SP05)"
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
