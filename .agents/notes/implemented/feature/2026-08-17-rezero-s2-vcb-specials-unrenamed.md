# 为 Re:零 第二季 VCB-Studio 特典文件增加未重命名识别规则

Status: implemented
Rule: `rezero-s2-vcb-specials-unrenamed`
Kind: `special_episode_unrenamed`
Generated-by: media-agent evolver (deepseek-v4-pro-0813)

## 现象

27个文件均为 VCB-Studio 发布的《Re:从零开始的异世界生活 第二季》特典内容（NCED、SP系列），位于 .other 或 .shorts 目录下，文件名保留了原始发布命名（如 [NCED02_EP45]、[SP05_01]），未转换为规范的 SxxExx 格式。现有规则 rezero-s2-extras-unrenamed 和 vcb-studio-sp-shorts-unrenamed 未能命中这些文件。

## 决定

新增一条声明式规则，通过 official_title、VCB-Studio 文件名模式、以及 .other/.shorts 目录三个条件组合来精确识别这批未重命名特典文件。不附加 action，因为无法可靠推断这些特典对应的规范集数编号，贸然重命名风险过高。规则仅用于让这些文件作为已知问题被追踪。

## 放弃的替代方案

考虑过直接基于文件名中的 NCED/SP 标记生成新名称，但 NCED 对应片尾曲、SP 对应特典短片，其规范集数映射（如对应哪一集的片尾或哪个特典编号）需要额外元数据，无法从文件名安全推导。也考虑过扩展现有 rezero-s2-extras-unrenamed 规则，但现有规则的匹配条件未知且已存在却未命中，新增独立规则更清晰可控。

## 风险与缓解

误伤风险低，因为匹配条件要求 official_title 精确等于该作品、文件名含 VCB-Studio 且位于 .other/.shorts 目录。若后续有其他 Re:零 第二季 VCB-Studio 特典文件被手动重命名，其文件名将不再含 [VCB-Studio] 或原始 NCED/SP 标记，不会命中此规则。

## 影子验证

```json
{
  "hits": 53,
  "covered_residue": true,
  "false_positives": 0,
  "false_positive_samples": [],
  "residue_size": 27,
  "overlap_with": null,
  "verdict": "PASS"
}
```

## 规则定义

```json
{
  "id": "rezero-s2-vcb-specials-unrenamed",
  "kind": "special_episode_unrenamed",
  "severity": "minor",
  "summary": "捕获 Re:从零开始的异世界生活 第二季 VCB-Studio 发布且位于 .other 或 .shorts 目录下未按规范命名的特典文件",
  "match": {
    "all": [
      {
        "field": "official_title",
        "op": "eq",
        "value": "Re:从零开始的异世界生活"
      },
      {
        "field": "filename",
        "op": "regex",
        "value": "\\[VCB-Studio\\].*(NCED|SP\\d{2}_\\d{2}).*\\.mkv$"
      },
      {
        "field": "parent_dir",
        "op": "in",
        "value": ".other|.shorts"
      }
    ]
  },
  "action": null,
  "source": "evolved",
  "enabled": true
}
```
