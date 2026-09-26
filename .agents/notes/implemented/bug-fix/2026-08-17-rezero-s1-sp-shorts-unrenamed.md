# 覆盖 Re:Zero 第一季 .shorts 中未重命名的 VCB-Studio SP 特典

Status: implemented
Rule: `rezero-s1-sp-shorts-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

Re:从零开始的异世界生活 的 .shorts 目录下有 25 个 VCB-Studio SP 特典文件，文件名保留原始发布命名（如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv），未按 {official_title} SxxExx 规范重命名。现有规则 vcb-studio-sp-shorts-unrenamed 和 rezero-s2-extras-unrenamed 均未覆盖这批第一季 SP 文件。

## 决定

新增一条声明式规则，精确匹配该 show_dir + .shorts + VCB-Studio SP 命名模式，将这批文件标记为 known issue。由于 SP 编号到规范季集号的映射关系不明确（SP01_07、SP02_14 等无法直接推导为 SxxExx），暂不附加 rename 动作。

## 放弃的替代方案

考虑过复用或扩展现有 vcb-studio-sp-shorts-unrenamed 规则，但其匹配条件未覆盖本簇样本，修改现有规则可能影响其他已规范文件；也考虑过直接给 rename 动作，但 SP 编号与目标集数的映射需要额外元数据确认，盲目重命名有误标风险。

## 风险与缓解

规则限定 show_dir 精确等于 'Re:从零开始的异世界生活' 且 season_dir 为 '.shorts'，并配合 VCB-Studio SP 文件名正则，误伤已规范文件的风险极低。不附加动作，仅做问题标记，无操作风险。

## 影子验证

```json
{
  "hits": 25,
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
  "id": "rezero-s1-sp-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:从零开始的异世界生活 第一季 .shorts 目录下的 VCB-Studio SP 特典文件未按规范重命名",
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
        "value": "\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP\\d{2}_\\d{2}\\]"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
