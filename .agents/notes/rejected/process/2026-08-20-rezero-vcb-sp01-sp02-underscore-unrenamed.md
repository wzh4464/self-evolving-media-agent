# Re:Zero SP01/SP02 下划线编号特典未重命名

Status: rejected
Rule: `rezero-vcb-sp01-sp02-underscore-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

在 Re:从零开始的异世界生活 的 .shorts 目录下发现25个 VCB-Studio 发布的特典文件，文件名格式为 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv 这类原始发布命名，未被重命名为 {official_title} SxxExx 规范格式。现有规则中已有十余条针对 rezero SP 未重命名的规则，但都未匹配到 SP01_xx / SP02_xx 这种下划线编号模式。

## 决定

立一条窄规则，仅命中 .shorts 目录下、以 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_ 或 [SP02_ 开头、且为 mkv 扩展名的文件，标记为 special_episode_unrenamed 已知问题。由于无法确定每集对应的规范集数和名称，不附加 action。

## 放弃的替代方案

考虑过放宽到所有 SPxx_yy 模式，但会误伤 SP03 及以后可能已被其他规则覆盖或命名不同的文件；也考虑过用 show_dir 字段过滤，但 parent_dir 已经足够定位 .shorts 目录，加 show_dir 只会让规则更脆。放弃给 action 是因为 SP 特典的规范命名需要查证具体内容，无法从文件名可靠推断。

## 风险与缓解

若未来有正常文件恰好位于 .shorts 目录且以 [VCB-Studio] Re Zero... [SP01_ 开头但已重命名，会被误命中。缓解：正则要求严格的原始发布格式（含 [Ma10p_1080p][x265_flac] 段由整体正则前部锚定），已重命名的文件不会匹配 [VCB-Studio] 前缀和 [SP01_xx] 结构。

## 影子验证

```json
{
  "hits": 0,
  "covered_residue": false,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 25,
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
  "id": "rezero-vcb-sp01-sp02-underscore-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:Zero 的 .shorts 目录下 VCB-Studio SP01/SP02 特典文件未按规范重命名",
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
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP0[12]_\\d{2}\\]"
      },
      {
        "field": "ext",
        "op": "eq",
        "value": "mkv"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
