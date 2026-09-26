# Re:Zero .shorts 下划线编号 SP 特典短篇未改名

Status: implemented
Rule: `rezero-vcb-shorts-sp-underscore-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

未被解释的异常样本显示，Re:Zero 的 .shorts 目录中有一批文件名仍为 [VCB-Studio] ...[SP01_07]...、[SP02_14]... 等格式的 mkv。它们位于 show_dir=Re:从零开始的异世界生活、season_dir=.shorts、parent_dir=.shorts，torrent 字段为空。现有大量 rezero special_episode_unrenamed 规则未覆盖这种 [SPxx_yy] 下划线编号形式。

## 决定

增加一条窄规则，只用 show_dir + season_dir + parent_dir + 严格文件名正则来锁定这批 [SPxx_yy] 格式的 .shorts 文件。不附带 rename，避免在无法确知 SPxx_yy 到 SxxExx 的正确映射时产生错误命名。

## 放弃的替代方案

考虑过 rename，但 SP01/SP02 与 _yy 的剧集编号映射不明确，乱改会二次污染；考虑过 trash，但这些是有效特典短篇，不应删除；考虑过 retag，但现有标签语义不足以提供可执行信息。

## 风险与缓解

误伤很低，因为规则同时限制为国语标题 show_dir、.shorts 目录和严格的 VCB-Studio 文件名正则；已规范成 {official_title} SxxExx 的文件不会匹配。

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
  "id": "rezero-vcb-shorts-sp-underscore-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "检测 Re:Zero .shorts 目录下 [SPxx_yy] 下划线编号的 VCB-Studio 特典短篇尚未改名",
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
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "^\\[VCB-Studio\\] Re Zero kara Hajimeru Isekai Seikatsu \\[SP[0-9]{2}_[0-9]{2}\\]\\[Ma10p_1080p\\]\\[x265_flac\\]\\.mkv$"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
