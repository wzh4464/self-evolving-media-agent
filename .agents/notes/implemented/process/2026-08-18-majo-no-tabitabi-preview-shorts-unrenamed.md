# 识别《魔女之旅》未规范化的预览短片

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《魔女之旅》的 .shorts 目录下存在12个 Preview 系列文件（如 [Airota&VCB-Studio] Majo no Tabitabi [Preview03][Ma10p_1080p][x265_flac].mkv），文件名保留原始发布组命名，未转换为 {official_title} SxxExx 规范格式。现有规则中的 preview_episode_unrenamed 规则未覆盖此作品。

## 决定

新增一条窄规则，仅匹配 show_dir=魔女之旅 且 season_dir=.shorts 且文件名含 [PreviewNN] 格式的文件，将其标识为已知的'预览短片未重命名'问题，不附加自动动作。

## 放弃的替代方案

考虑过泛化一条覆盖所有作品的 .shorts Preview 规则，但现有规则已按作品拆分（如 vcb-studio-preview-shorts-unrenamed），且泛化规则可能误伤其他作品的规范文件，故放弃。也考虑过为这批文件自动重命名为 S00E 格式，但 Preview 编号与正式集数的映射关系不明确，硬写 rename 参数会出错，故仅报告问题。

## 风险与缓解

规则限定 show_dir=魔女之旅 且 season_dir=.shorts 且文件名含 [PreviewNN]，三重条件交集极窄。若未来该作品的正片文件也放入 .shorts 且文件名恰好含 [PreviewNN]（几乎不可能），会误命中，但风险可忽略。

## 影子验证

```json
{
  "hits": 12,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 12,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "majo-no-tabitabi-preview-shorts-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "识别《魔女之旅》.shorts 目录下未按规范命名的 Preview 预览短片",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Preview\\d{2}\\]"
      },
      {
        "field": "filename",
        "op": "not_regex",
        "value": "^魔女之旅 S\\d{2}E\\d{2}"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
