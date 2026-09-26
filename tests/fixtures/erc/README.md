# 真实 ERC 报告 fixture（T06）

这三份 JSON 都是 **kicad-cli 10.0.6 真实产出**，不是手写的。用途是验 T06 验收
里那句"用真实有效报告 fixture 验证不会把合法零违规报告误拒绝"——手写 fixture
只能证明解析器同意我的想象，真实报告才能证明它同意工具的行为。

| 文件 | 违规 | 特殊之处 |
|---|---|---|
| `real-clean.json` | 无 | 生产工程原样跑出来的报告（0 错误 0 警告） |
| `real-warning-and-error.json` | 1 error + 2 warning | 同一份报告里既有 error 也有 warning |
| `real-ignored-important.json` | 无 | `ignored_checks` 里多了一个 `pin_not_connected` |

`manifest.json` 记了每份的 sha256 与"从哪里来"，测试会核对——fixture 不能
被悄悄改掉。

## 怎么造出来的

三份都从一个**完整工程副本**跑出来（`.kicad_sch` + `.kicad_pro` +
`circuitos.kicad_sym` + `sym-lib-table` 四个文件齐全）：

```
python output/t06-evidence/_learn/h1_probe.py
```

只拷 `.kicad_sch` 会得到 17 条 `lib_symbol_issues` 告警——那是"缺符号库表"的
假象，不是电路问题（T06 踩过这个坑）。所以探针固定整目录拷贝。

各份的差异：

- `real-clean.json`：原样，什么都不改。**0 违规**——证明前面那 17 条告警确实
  只是缺库造成的。
- `real-warning-and-error.json`：在 `.kicad_pro` 里写
  `erc.rule_severities.pin_not_connected = "warning"`，再删掉一条顶层
  `(wire ...)`（130 字节），把电源符号岛弄断。于是得到 2 条真实
  `pin_not_connected` **告警** + 1 条真实 `power_pin_not_driven` **错误**。
- `real-ignored-important.json`：在 `.kicad_pro` 里写
  `erc.rule_severities.pin_not_connected = "ignore"`，图保持完整。于是
  `ignored_checks` 里多出 `pin_not_connected`——**工具少跑了一项检查**。

## 两个实测结论（T06 的证据基础）

1. **kicad-cli 认 `.kicad_pro` 里的 `erc.rule_severities`**。这是 H1/H2 假设，
   探针跑了才知道：改 warning，报告里 severity 就是 warning；改 ignore，该项
   进 `ignored_checks` 且不出违规。
2. **被禁用的检查会让"没违规"变成一句空话**。H2b（ignore + 删导线）里导线
   真的断了，`pin_not_connected` 却**一声不吭**，只剩 `power_pin_not_driven`
   还在报。这正是 `W-ERC-003` 要拦的东西：结论必须暴露它不覆盖哪些检查。

## 重新生成会得到什么（实测）

重跑 `h1_probe.py` **不会**得到逐字节相同的报告——`date` 会变（工具写当前
时间）。实测对比（2026-09-26）：

| fixture | 逐字节相同 | 违规指纹相同 | date |
|---|---|---|---|
| `real-clean.json` | 否 | 是 | 12:17:36 → 12:24:13 |
| `real-warning-and-error.json` | 否 | 是 | 12:17:45 → 12:24:23 |
| `real-ignored-important.json` | 否 | 是 | 12:17:49 → 12:24:27 |

"违规指纹"= 按 (规则, 级别, 对象 uuid 集合) 排序后的集合。也就是说重跑得到的是
**电气上等价**的报告，不是同一份字节。所以这里放的是**冻结的副本**（`manifest.json`
用 sha256 钉住），重新生成后要更新哈希——测试会核对，改而不更新立刻红。

## 换 fixture 时要做什么

重新生成后更新 `manifest.json` 的 sha256，并确认上面的表格仍然成立——
测试 `TestRealReportFixtures` 会核对哈希，改而不更新会立刻红。
