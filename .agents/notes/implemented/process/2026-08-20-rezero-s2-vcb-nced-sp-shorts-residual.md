# 为 rezero-s2 VCB 特典残留补充窄规则

Status: implemented
Rule: `rezero-s2-vcb-nced-sp-shorts-residual`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

库中仍有 27 个 Re:从零开始的异世界生活第二季特典文件保持 VCB-Studio 原始命名，分别位于 .other（NCED02_EP45/EP47）和 .shorts（SP03_04/SP03_07/SP03_09/SP05_01）目录。已有规则 rezero-s2-vcb-nced-sp03-sp05-final、rezero-s2-vcb-nced-sp03-sp05-other-shorts 等未命中它们，说明此前规则的正则范围未覆盖这批具体编号。

## 决定

新增一条 narrow regex 规则，只匹配该剧第二季 VCB-Studio 命名中以 NCED02_EP45、NCED02_EP47、SP03_xx、SP05_xx 形式出现的文件，并要求 season_dir 为 .other 或 .shorts。暂不附加动作，因为目前缺少权威命名映射，不确定这些特典应映射到哪些 SxxExx 编号。

## 放弃的替代方案

考虑过直接复用现有 rezero-s2-vcb-nced-sp03-sp05-* 规则并扩展其正则范围，但会改动已稳定的规则，风险更大；也考虑过按目录分别立规则，但样本显示 .other 和 .shorts 都指向同一类未命名特典，合并为一条更清晰。

## 风险与缓解

正则限定在 VCB-Studio 第二季特典编号范围内，且要求 show_dir 精确匹配官方标题，误伤正片或已规范文件的可能性很低。未附 action 避免了错误重命名的风险。

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
  "id": "rezero-s2-vcb-nced-sp-shorts-residual",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取 Re:从零开始的异世界生活第二季 VCB-Studio 版中仍以 NCED/SP 原始编号命名、位于 .other 或 .shorts 目录下的未规范命名特典。",
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
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season \\[(NCED02_EP4[57]|SP0[35]_\\d{2})\\]"
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
