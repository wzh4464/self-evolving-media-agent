# 为 Re:Zero 第二季特典文件补一条未重命名规则

Status: implemented
Rule: `rezero-s2-extras-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

27 个文件来自 VCB-Studio 发布的 Re:Zero 第二季资源，文件名保留原始罗马音（Re Zero kara Hajimeru Isekai Seikatsu 2nd Season）和压制组标记，未按 {official_title} SxxExx.ext 规范重命名。内容为 NCED（无字幕片尾）和 SP（特典短片），分布在 .other 和 .shorts 目录。现有规则 vcb-studio-sp-shorts-unrenamed 和 vcb-studio-preview-shorts-unrenamed 均未命中，可能因为它们的匹配模式针对其他标题或编号格式。

## 决定

立一条窄规则，精确匹配 official_title 为 Re:从零开始的异世界生活 且文件名含 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu 2nd Season [NCED 或 [SP 的文件，标记为 special_episode_unrenamed。不附带动作，因为这些特典没有标准的 SxxExx 编号可映射，贸然重命名可能造成编号冲突或信息丢失。

## 放弃的替代方案

考虑过泛化规则来覆盖所有 VCB-Studio 的 NCED/SP 未重命名文件，但因其他番剧的样本不足，泛化会引入误伤风险。也考虑过按目录（.other 和 .shorts）匹配，但目录名过于通用，其他已规范的文件也可能位于这些目录。

## 风险与缓解

误伤风险低：规则同时限定 official_title 和文件名中的完整罗马音标题及 NCED/SP 编号模式，双重条件确保只命中该番剧该季度的 VCB-Studio 特典。若未来有其他压制组使用相同命名模式，不会被误命中，因为压制组标记 [VCB-Studio] 是必须的。

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
  "id": "rezero-s2-extras-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "命中 Re:Zero 第二季中未重命名的 NCED 和 SP 特典文件（VCB-Studio 原始命名）",
  "match": {
    "all": [
      {
        "field": "official_title",
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
