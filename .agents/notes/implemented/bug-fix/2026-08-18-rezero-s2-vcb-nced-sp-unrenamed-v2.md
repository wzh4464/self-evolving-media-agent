# 补漏 Re:Zero S2 [VCB-Studio] NCED/SP 特别篇未重命名文件

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-unrenamed-v2`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

现有规则 rezero-s2-vcb-nced-sp-unrenamed 和 rezero-s2-vcb-specials-unrenamed 应该覆盖这批文件，但实际未命中。观察样本发现这批文件带有子编号后缀（EP45/EP47、SP03_04/SP03_07/SP03_09、SP05_01），推测现有规则的 match 只匹配了不含子编号的纯 NCED/SP 前缀模式。这些文件分布在 .other 和 .shorts 两个特殊目录。

## 决定

新增一条规则，用更宽的 regex 捕获 `NCED\d+_EP\d+` 和 `SP\d+_\d+` 两种带子编号的命名模式，同时限定 show_dir 为 Re:从零开始的异世界生活、parent_dir 为 .other 或 .shorts，确保只命中这批已知的 Re:Zero S2 VCB 特别篇文件。不给 action，因为这些是特别篇/NCED，正确的重命名目标（对应季集或特殊编号）无法从文件名可靠推断，需要人工确认。

## 放弃的替代方案

考虑过直接修改现有 rezero-s2-vcb-nced-sp-unrenamed 规则的 match 条件来扩宽覆盖，但无法确认现有规则当初的精确 match 范围，贸然修改可能影响已稳定的行为，新增独立 v2 规则更安全。也考虑过用 retag 打上 'needs-rename' 标签，但规则本身 kind 已表达问题类型，冗余动作无必要。

## 风险与缓解

regex 限定 show_dir 和 official_title 均为 Re:从零开始的异世界生活，且 parent_dir 必须是 .other 或 .shorts，误伤已规范文件的风险极低。唯一残余风险是未来用户手动把文件移到正式季目录后规则不再命中，但那时文件已脱离本规则针对的'散落在特殊目录'场景，属于预期行为。

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
  "id": "rezero-s2-vcb-nced-sp-unrenamed-v2",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "命中 Re:Zero 第二季 [VCB-Studio] 发布的 NCED/SP 特别篇未重命名文件",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "official_title",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED\\d+_EP\\d+|SP\\d+_\\d+)\\]"
      },
      {
        "field": "parent_dir",
        "op": "in",
        "value": ".other,.shorts"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
