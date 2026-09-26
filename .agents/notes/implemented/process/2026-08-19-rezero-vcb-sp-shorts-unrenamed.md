# 补齐 Re:从零 VCB SP 短片的命名规范覆盖

Status: implemented
Rule: `rezero-vcb-sp-shorts-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

在 Re:从零开始的异世界生活 的 .shorts 目录下，发现 25 个文件仍保留 VCB-Studio 原始命名（如 [VCB-Studio] Re Zero kara Hajimeru Isekai Seikatsu [SP01_07][Ma10p_1080p][x265_flac].mkv），没有按 {official_title} SxxExx.ext 规范重命名，且现有 32 条规则中虽有多个 rezero special_episode_unrenamed 规则，但均未命中这批文件。

## 决定

新增一条窄规则，用 parent_dir=.shorts + filename 正则 ^\[VCB-Studio\].*\[SP\d{2}_\d{2}\].*\.mkv$ + show_dir=Re:从零开始的异世界生活 三个条件锁定这批文件，使其成为可追踪的已知问题。由于当前无法从文件名可靠推断 SP 编号到目标季/集的映射，暂不给 action。

## 放弃的替代方案

考虑过：(1) 放宽 parent_dir 匹配以覆盖其他目录，但样本全部在 .shorts 下，放宽会增加误伤风险；(2) 复用或扩展已有 rezero SP 规则，但已有规则的具体匹配条件不可见，贸然修改可能破坏其既有覆盖；(3) 直接给 rename 动作，但 SPxx_yy 到规范命名（哪一季的哪一集）缺少可靠映射依据，硬写会产出错误命名。

## 风险与缓解

误伤风险低：三个条件同时满足才能命中，其中 show_dir 精确匹配、parent_dir 精确匹配、filename 正则要求以 [VCB-Studio] 开头且含 [SP数字_数字] 格式并以 .mkv 结尾。若未来有已规范重命名的文件仍保留 [SPxx_yy] 子串则可能被误报，但按规范重命名后的文件不会以 [VCB-Studio] 开头，因此实际误伤概率极低。

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
  "id": "rezero-vcb-sp-shorts-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "important",
  "summary": "识别 .shorts 目录下仍保留 VCB-Studio 原始命名且含 SP 标记的 Re:从零 特典文件",
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
      },
      {
        "field": "show_dir",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
