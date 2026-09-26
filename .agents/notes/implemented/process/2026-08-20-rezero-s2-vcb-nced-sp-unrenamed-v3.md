# 补充 Re:Zero S2 特典未重命名规则

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-unrenamed-v3`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

发现 8 个 Re:Zero 第二季的 NCED 和 SP 特典文件，命名不符合规范且未被现有规则覆盖。

## 决定

添加一条规则，匹配 show_dir 为 Re:从零开始的异世界生活 且文件名以 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season [NCED 或 [SP 开头的文件，标记为 special_episode_unrenamed。

## 放弃的替代方案

考虑过按 parent_dir 区分 .other 和 .shorts，但文件名模式已足够精确，无需额外条件。

## 风险与缓解

可能误伤其他类似命名但已规范的文件，但通过 show_dir 和文件名正则双重限制，风险较低。

## 影子验证

```json
{
  "hits": 8,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 8,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "rezero-s2-vcb-nced-sp-unrenamed-v3",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "标记 Re:Zero 第二季 VCB-Studio 的 NCED 和 SP 特典文件未重命名",
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
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED|SP)\\d+"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
