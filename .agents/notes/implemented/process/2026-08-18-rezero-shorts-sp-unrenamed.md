# 补全 Re:从零开始的异世界生活 .shorts 特典未重命名规则的覆盖缺口

Status: implemented
Rule: `rezero-shorts-sp-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

媒体库中存在 25 个位于 Re:从零开始的异世界生活/.shorts/ 目录下的 VC-B-Studio 特典短片，文件名保留原始发布格式（如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv），未按 {official_title} SxxExx.ext 规范重命名。现有规则中虽有多条针对该作品特典的 unrenamed 规则，但匹配范围均未覆盖 .shorts 目录下带 SP 编号的这批文件。

## 决定

新增一条声明式规则，精确匹配 show_dir 为该作品、season_dir 为 .shorts、文件名以 [VCB-Studio] 开头且包含 [SPxx_xx] 模式的文件，将其标记为 special_episode_unrenamed。不附带 action，因为无法从原始文件名可靠推断出目标命名所需的正确季集号（SP01_07 中的数字语义需要人工确认）。

## 放弃的替代方案

考虑过复用现有 rezero-s1-sp-shorts-unrenamed 规则并扩展其匹配条件，但该规则匹配范围不明确且已有 v2 版本，扩展现有规则可能误伤其他目录下已规范的文件。也考虑过直接给出 rename action，但由于 SP 编号与季集号的映射关系不明确，强行重命名风险过高，故放弃。

## 风险与缓解

误伤风险极低。匹配条件同时限定 show_dir、season_dir 和文件名正则，三者缺一不可。正则 ^\[VCB-Studio\] 锚定文件名开头，[SP\d{2}_\d{2}] 精确匹配 SP 编号模式，不会命中已重命名为 {official_title} SxxExx.ext 的规范文件。若未来该目录下出现已规范命名的文件，因其文件名不以 [VCB-Studio] 开头，不会被误命中。

## 影子验证

```json
{
  "hits": 50,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 25,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "rezero-shorts-sp-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "标记 Re:从零开始的异世界生活 .shorts 目录下未重命名的 SP 编号特典短片",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\].*\\[SP\\d{2}_\\d{2}\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
