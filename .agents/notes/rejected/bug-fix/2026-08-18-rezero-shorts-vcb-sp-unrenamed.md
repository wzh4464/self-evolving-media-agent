# 识别 Re:从零开始的异世界生活 .shorts 目录下的 VCB-Studio SP 特典未重命名文件

Status: rejected
Rule: `rezero-shorts-vcb-sp-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

25个文件位于 Re:从零开始的异世界生活 的 .shorts 目录，文件名均为 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SPxx_xx][Ma10p_1080p][x265_flac].mkv 模式，torrent 相关字段为空，说明可能是手动导入或订阅中断后的残留。现有规则中虽有多个 rezero special_episode_unrenamed 规则，但均未覆盖 .shorts 目录下的 SP 编号命名。

## 决定

新增一条规则，精确匹配 show_dir 为 Re:从零开始的异世界生活、season_dir 为 .shorts、文件名以 [VCB-Studio] Re Zero 开头且包含 [SPxx_xx] 模式的 mkv 文件，将其标记为 special_episode_unrenamed 已知问题。暂不附加 action，因为无法确定这些 SP 特典的具体 TMDB 编号和正确命名。

## 放弃的替代方案

1) 扩展已有的 rezero-s1-sp-shorts-unrenamed-v2 规则——放弃，因为该规则可能匹配其他命名模式，修改风险较高。2) 使用更宽泛的 match（如只匹配 .shorts 目录）——放弃，会误伤该目录下可能已规范的文件。3) 附加 rename action——放弃，SP 特典的规范命名需要逐集确认 TMDB 元数据，无法自动推导。

## 风险与缓解

该规则要求文件名严格以 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP 开头，且限定在 .shorts 目录，误伤概率极低。若未来这些文件被正确重命名，规则自然不再命中。

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
  "id": "rezero-shorts-vcb-sp-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "Re:从零开始的异世界生活 .shorts 目录下的 VCB-Studio SP 特典文件未按规范重命名",
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
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP\\d{2}_\\d{2}\\]"
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
