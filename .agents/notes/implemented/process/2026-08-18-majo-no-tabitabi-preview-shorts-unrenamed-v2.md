# 补漏：魔女之旅 Preview 特典短片未被现有规则捕获

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-unrenamed-v2`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《魔女之旅》的 12 个 Preview 特典文件存放在 .shorts 目录，文件名保留发布组原始命名（如 [Airota&VCB-Studio] Majo no Tabitabi [Preview03][Ma10p_1080p][x265_flac].mkv），均未按 {official_title} SxxExx 规范重命名。现有规则 majo-no-tabitabi-preview-shorts-unrenamed 和 vcb-studio-preview-shorts-unrenamed 存在但未命中此簇。

## 决定

新增规则 majo-no-tabitabi-preview-shorts-unrenamed-v2，以 season_dir=.shorts + filename 含 [PreviewNN] + filename 含 'Majo no Tabitabi' 三条件联合匹配，只标记问题，不附加动作（Preview 特典无标准集数编号，无法确定 SxxExx 目标名，留待人工处理）。

## 放弃的替代方案

考虑过直接复用或修改现有 majo-no-tabitabi-preview-shorts-unrenamed 规则，但无法查看其具体匹配条件，且修改可能影响其他已正常处理的文件，故选择新增窄规则。也考虑过给 rename 动作，但 Preview 编号到 SxxExx 的映射无可靠依据，贸然重命名会引入错误，故省略 action。

## 风险与缓解

风险在于若未来有其他作品的 .shorts 目录下文件名同时含 'Majo no Tabitabi' 和 [PreviewNN]，会一并命中。但 'Majo no Tabitabi' 是具体作品名，误伤概率极低；且规则只报告不操作，即使误报也无副作用。

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
  "id": "majo-no-tabitabi-preview-shorts-unrenamed-v2",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "《魔女之旅》.shorts 目录下保留发布组原始命名的 Preview 特典文件未重命名",
  "match": {
    "all": [
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
        "op": "contains",
        "value": "Majo no Tabitabi"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
