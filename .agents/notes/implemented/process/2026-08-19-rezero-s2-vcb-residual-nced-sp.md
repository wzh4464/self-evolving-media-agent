# Re:Zero S2 VCB 残留特典文件命名规范未覆盖

Status: implemented
Rule: `rezero-s2-vcb-residual-nced-sp`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

27 个 VCB-Studio 第二季特典文件（NCED02_EP45、NCED02_EP47、SP03_xx、SP05_xx 等）残留在 .other 与 .shorts 目录，文件名仍为发布组原始命名，未按官方标题规范重命名。现有多个 rezero-s2 相关规则均未命中这批样本。

## 决定

新增一条窄规则，用正则精确匹配 VCB-Studio Re Zero 2nd Season 的 NCED/SP 方括号标签，并限定 parent_dir 为 .other 或 .shorts，将其暴露为已知的 special_episode_unrenamed 问题。

## 放弃的替代方案

考虑过复用现有 rezero-s2 规则并扩展其匹配条件，但那些规则各自针对不同子集（如 S1 SP、vcb-nced-sp 等），扩展可能引入误伤；也考虑过不设 parent_dir 限制，但这样会命中已规范目录中同命名模式的文件，风险过高。

## 风险与缓解

误伤风险低：正则同时要求 VCB-Studio 前缀、Re Zero 2nd Season 标题、NCED/SP 方括号标签、以及 .other/.shorts 父目录，四重条件交集极为狭窄。若未来这些文件被正确重命名或移入规范目录，规则将自然停止命中，不会影响已规范文件。

## 影子验证

```json
{
  "hits": 25,
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
  "id": "rezero-s2-vcb-residual-nced-sp",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取 Re:从零开始的异世界生活第二季 VCB-Studio 残留的 NCED/SP 特典文件",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP)\\d+_\\d+\\]"
      },
      {
        "field": "parent_dir",
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
