# 补充 Re:从零开始的异世界生活 .shorts 目录下 [SPxx_yy] 格式特典的未改名识别

Status: rejected
Rule: `rezero-vcb-shorts-sp-numeric-underscore-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

25 个文件位于 Re:从零开始的异世界生活/.shorts/，文件名形如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv，均以 [VCB-Studio] 前缀加罗马音标题加 [SPxx_yy] 标记命名。现有大量 rezero special_episode_unrenamed 规则均未命中，说明这些规则匹配的模式（可能是 SP 后无下划线数字、或无 [VCB-Studio] 前缀、或不同目录层级）与这批文件的 [SP01_07] 这种数字_数字格式不重叠。

## 决定

新增一条 narrow 规则，精确限定 show_dir、season_dir、文件名正则（[VCB-Studio] 前缀 + 罗马音标题 + [SP 数字_数字]）和 ext，将这些文件标记为 special_episode_unrenamed 问题。不附带 action，因为无法从现有信息推断正确的 SxxExx 编号。

## 放弃的替代方案

考虑过合并进现有 rezero-vcb-shorts-sp01-sp02-unrenamed 或 rezero-vcb-sp-shorts-unrenamed 规则，但现有规则未命中这批文件，说明其匹配条件与此处不兼容，贸然扩展现有规则有误伤风险，故放弃。也考虑过给 rename 动作，但无法确定 SP01_07 对应的正确季集号，硬凑 new_name 会被驳回，故省略 action。

## 风险与缓解

正则锚定在文件名开头且限定 show_dir 和 season_dir，误伤风险极低。若未来同名番剧的其他目录层级也出现类似命名，本规则不会命中，这属于窄规则的预期行为。

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
  "id": "rezero-vcb-shorts-sp-numeric-underscore-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "命中 Re:从零开始的异世界生活 .shorts 目录下 [VCB-Studio] 开头、带 [SPxx_yy] 数字下划线标记的未改名特典文件",
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
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP\\d+_\\d+\\]"
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
