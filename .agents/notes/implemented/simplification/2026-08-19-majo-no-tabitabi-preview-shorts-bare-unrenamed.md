# 放宽Preview未重命名规则的匹配条件

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-bare-unrenamed`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅.shorts目录下存在12个Preview特典文件（如Preview03、Preview05、Preview06、Preview08、Preview10、Preview11），文件名保留原始发布命名`[Airota&VCB-Studio] Majo no Tabitabi [PreviewXX][Ma10p_1080p][x265_flac].mkv`，未按`魔女之旅 SxxExx`规范重命名，且torrent_name为空。现有规则中有多条针对majo-no-tabitabi-preview-shorts的规则，但这些规则可能依赖了更具体的torrent标签、路径或文件列表，导致这批纯文件层面的残留未被任何规则覆盖。

## 决定

新增一条直接基于show_dir、season_dir和filename模式的规则，不依赖torrent字段（除torrent_name为空作为过滤条件），确保只要文件还带着原始Preview命名就能被识别为已知问题。

## 放弃的替代方案

考虑过修改现有某条majo-no-tabitabi-preview-shorts规则使其更宽泛，但现有规则可能服务于不同的torrent状态场景，贸然改动会影响其他批次。也考虑过加rename动作，但Preview片段的正确集数映射（Preview03对应SxxExx的哪个Exx）无法从文件名确定，需要人工核对，故省略action。

## 风险与缓解

误伤风险低——正则`\[Preview\d{2}\]`只匹配带Preview两位数字标记的文件，且限定在魔女之旅的.shorts目录下。若后续这些文件被正确重命名（如`魔女之旅 S01E01.mkv`），则不再命中此规则，符合预期。

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
  "id": "majo-no-tabitabi-preview-shorts-bare-unrenamed",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "捕获《魔女之旅》.shorts目录下仍未重命名的Preview片段文件",
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
        "field": "torrent_name",
        "op": "eq",
        "value": ""
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
