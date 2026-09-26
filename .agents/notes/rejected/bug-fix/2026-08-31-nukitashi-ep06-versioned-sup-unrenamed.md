# 识别拔作岛版本化sup字幕未重命名

Status: rejected
Rule: `nukitashi-ep06-versioned-sup-unrenamed`
Kind: `subtitle_version_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《住在拔作岛上的我应该如何是好？》第06集有 6 个 .sup 中文字幕文件，文件名保留发布源的【全面限制 ver.】【配信限定 ver.】【青蓝岛 ver.】和简繁标记，未按 official_title 规范重命名，现有 unrenamed/sidecar 规则未命中。

## 决定

增加一条窄规则，按剧名、sup 扩展名和文件名中三个特定版本后缀精确匹配这批文件，不附加自动动作，仅将其标记为已归类问题。

## 放弃的替代方案

考虑过泛化版本后缀以覆盖其他集数，但样本仅第06集且为防误伤暂不扩大；自动重命名缺少规范目标名和版本取舍依据，故不产生动作。

## 风险与缓解

误伤风险低，因为同时约束剧名、扩展名和三个特定版本后缀；不会命中已规范命名文件。同剧其他集数若出现相同模式需后续补规则。

## 影子验证

```json
{
  "hits": 0,
  "covered_residue": false,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 6,
  "overlap_with": null,
  "verdict": "REJECT",
  "reject_reasons": [
    "没有覆盖它本该解决的样本",
    "一个都没命中"
  ]
}
```

## 规则定义

```json
{
  "id": "nukitashi-ep06-versioned-sup-unrenamed",
  "kind": "subtitle_version_unrenamed",
  "severity": "minor",
  "summary": "识别拔作岛第06集带发布版本后缀的中文sup字幕文件未按规范重命名",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "住在拔作岛上的我应该如何是好？"
      },
      {
        "field": "ext",
        "op": "eq",
        "value": "sup"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^【7月】NUKITASHI住在拔作岛上的我该如何是好？ 06【(全面限制|配信限定|青蓝岛) ver\\.】\\.中文（(简体|繁體)）\\.sup$"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true,
  "resolution": "classified"
}
```
