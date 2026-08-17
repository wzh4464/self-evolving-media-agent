# 识别 VCB-Studio 压制组原始命名未规范化的特典文件

Status: rejected
Rule: `unrenamed-vcb-raw`
Kind: `unrenamed_release`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

27 个文件以 [VCB-Studio] 原始命名格式存在，分布在 Re:从零开始的异世界生活 的 .other 和 .shorts 目录中，包含 NCED 和 SP 特典内容。文件名保留压制组格式，与媒体库 `{official_title} SxxExx.ext` 规范不一致，但现有规则（unrenamed-file、extras-in-library 等）均未命中。

## 决定

添加一条窄规则，专门匹配 VCB-Studio 的 Re:Zero 原始命名格式，且限定在 .other 和 .shorts 目录，将这些文件标记为已知的未规范化问题。不给 action，因为无法从文件名可靠推断出 SxxExx 编号（NCED02_EP45 映射到哪一集需要人工确认，SP03/SP05 的特典编号规范也未知）。

## 放弃的替代方案

考虑过用更宽泛的 [VCB-Studio] 前缀匹配来覆盖所有压制组原始命名，但这样可能误伤其他已正确归类的番剧；也考虑过直接用 rename 动作，但 NCED 和 SP 编号与正片集数的映射关系不明确，强行重命名会制造错误。

## 风险与缓解

规则限定 show_dir 必须为 Re:从零开始的异世界生活，且 season_dir 必须是 .other 或 .shorts，误伤风险极低。若未来该番剧的正片文件也以 VCB-Studio 原始命名存在但放在标准季目录中，本规则不会命中，属于可接受的漏报。

## 影子验证

```json
{
  "hits": 83,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 27,
  "overlap_with": "extras-in-library",
  "verdict": "REJECT",
  "reject_reasons": [
    "命中 83 个，远超残留规模 27，规则过宽",
    "与现有规则 extras-in-library 重叠"
  ]
}
```

## 规则定义

```json
{
  "id": "unrenamed-vcb-raw",
  "kind": "unrenamed_release",
  "severity": "minor",
  "summary": "命中 VCB-Studio 原始命名格式的 Re:Zero 番剧文件，标记为待规范化的已知问题",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu"
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
