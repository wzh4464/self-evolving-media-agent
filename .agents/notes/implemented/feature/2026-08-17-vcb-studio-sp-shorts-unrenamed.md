# 识别 .shorts 目录下 VCB-Studio SP 特典未重命名文件

Status: implemented
Rule: `vcb-studio-sp-shorts-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

《Re:从零开始的异世界生活》的 .shorts 目录中存在 25 个文件，文件名保留着 VCB-Studio 压制组的原始命名格式（含 [SPxx_xx] 标记），未按库内 `{official_title} SxxExx.ext` 规范重命名，现有规则均未覆盖。

## 决定

新增声明式规则，只匹配 parent_dir 为 .shorts 且文件名符合 VCB-Studio SP 特典正则格式的 .mkv 文件，将其标记为已知问题。暂不附加动作，因为无法从现有字段确定 SP 特典对应的正确季集编号。

## 放弃的替代方案

考虑过用 season_dir 匹配以覆盖更多目录，但 .shorts 是当前观察到的唯一场景，放宽到 season_dir 会增加误伤风险；也考虑过给 rename 动作，但 SP 编号到规范季集号的映射关系不明确，贸然重命名会造成错误。

## 风险与缓解

若其他剧集也有 .shorts 目录且包含合法命名的 VCB-Studio SP 文件，会被误标。缓解措施：match 同时限定 parent_dir 和严格的 VCB-Studio SP 正则，且规则仅为标记性质，不执行任何变更动作。

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
  "id": "vcb-studio-sp-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "抓取 .shorts 目录下 VCB-Studio 特典命名格式（SPxx_xx）未按官方标题规范重命名的文件",
  "match": {
    "all": [
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\].*\\[SP\\d{2}_\\d{2}\\].*\\.mkv$"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
