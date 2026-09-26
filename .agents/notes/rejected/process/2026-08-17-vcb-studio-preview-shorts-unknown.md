# 登记 VCB-Studio Preview 预告片为已知未重命名问题

Status: rejected
Rule: `vcb-studio-preview-shorts-unknown`
Kind: `preview_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

魔女之旅的 .shorts 目录下有 12 个以 [Airota&VCB-Studio] 开头、文件名包含 [PreviewNN] 的 mkv 文件，全部未按 {official_title} SxxExx 规范重命名，且现有规则（含 vcb-studio-preview-shorts-unrenamed）均未命中。torrent 相关字段为空，说明这些文件可能并非由 torrent 导入，或 torrent 信息已丢失。

## 决定

新增规则抓取 .shorts 目录下、文件名匹配 [Airota&VCB-Studio]...[PreviewNN]...mkv 的文件，将它们标记为 preview_episode_unrenamed 类已知问题。不附带强制动作，避免在无法确定正确剧集编号时产生错误的 NFO 或重命名。

## 放弃的替代方案

考虑过直接重命名为 S00E 系列，但 Preview 短片通常没有官方剧集编号，强行编号可能造成与真正的 SP/特典混淆。也考虑过 trash，但 Preview 内容可能仍有一定收藏价值，删除过于激进。

## 风险与缓解

正则锚定 [Airota&VCB-Studio] 开头和 [PreviewNN] 结构，且限定 season_dir 为 .shorts，误伤面很窄。唯一风险是其他番剧的 .shorts 目录下若有同发布组 Preview 文件也会被命中，但这属于同一类问题，命中是合理的。

## 影子验证

```json
{
  "verdict": "REJECT",
  "hits": 0,
  "reject_reasons": [
    "动作参数不合契约：动作 `write_nfo` 缺少必需参数 ['title', 'tmdb_id']（给出的是 []）"
  ]
}
```

## 规则定义

```json
{
  "id": "vcb-studio-preview-shorts-unknown",
  "kind": "preview_episode_unrenamed",
  "severity": "minor",
  "summary": "命中 .shorts 目录下 Airota&VCB-Studio 发布的 Preview 预告片文件",
  "match": {
    "all": [
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[Airota&VCB-Studio\\].*\\[Preview\\d{2}\\].*\\.mkv$"
      },
      {
        "field": "season_dir",
        "op": "eq",
        "value": ".shorts"
      }
    ]
  },
  "action": {
    "op": "write_nfo",
    "args": {},
    "note": "Preview 短片无标准 TMDB 剧集编号，暂不写入占位 NFO；仅登记为已知问题，待后续人工确认是否保留或归档。"
  },
  "source": "evolved",
  "enabled": true
}
```
