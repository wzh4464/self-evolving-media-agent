# 魔女之旅 Preview 系列 shorts 仍未被现有规则覆盖

Status: implemented
Rule: `majo-no-tabitabi-preview-shorts-preview-series`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

「魔女之旅」的 .shorts 目录下存在 12 个 Airota&VCB-Studio 发布的 Preview 系列文件（Preview03/05/06/08/10/11 等），文件名均为原始发布命名，不符合 {official_title} SxxExx.ext 规范。现有规则中已有多条针对 preview_episode_unrenamed 和 special_episode_unrenamed 的规则，但均未命中这批文件——它们形成了一个新的、未被命名的异常簇。

## 决定

新增一条声明式规则，精确匹配「魔女之旅 + .shorts 目录 + Airota&VCB-Studio Preview 序列」这一组合，将这批文件标记为 preview_episode_unrenamed 类型的已知问题。不附加 action，因为 Preview 集数映射到官方 SxxExx 编号需要人工核对，无法从现有字段可靠推导。

## 放弃的替代方案

考虑过放宽规则匹配所有 .shorts 下的 Preview 文件，但这样会命中其他已由现有规则覆盖或已规范化的作品，误伤面过大。也考虑过给 rename 动作，但 PreviewNN 到官方季集号的映射没有可靠依据，硬写 new_name 会引入错误命名，放弃。

## 风险与缓解

规则使用 show_dir、parent_dir、filename 三重约束，且 regex 精确到发布组名、Preview 编号格式和编码参数，误伤风险极低。若未来同一发布组对魔女之旅的其他 shorts 目录也有类似命名，本规则不会误命中——这正是期望的窄覆盖。

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
  "id": "majo-no-tabitabi-preview-shorts-preview-series",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "命中魔女之旅 .shorts 目录下 Airota&VCB-Studio 发布的 Preview 系列文件（现有规则均未覆盖的残余异常簇）",
  "match": {
    "all": [
      {
        "field": "show_dir",
        "op": "eq",
        "value": "魔女之旅"
      },
      {
        "field": "parent_dir",
        "op": "eq",
        "value": ".shorts"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Airota&VCB-Studio\\] Majo no Tabitabi \\[Preview\\d{2}\\]\\[Ma10p_1080p\\]\\[x265_flac\\]\\.mkv"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
