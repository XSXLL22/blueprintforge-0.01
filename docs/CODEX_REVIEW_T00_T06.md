# T00–T06 独立复核报告

> **2026-09-26 修复后复核：两个 P1 均已关闭。** DeepSeek 新增的
> `CatalogValidationPolicy` 已通过 `resolve()` 接入校验、渲染和落盘路径；损坏
> 的 EP=10 目录会被 `E-CAT-002` 阻断，严格口径无法读取封装时会被
> `E-CAT-006` 阻断。`connection_rules`、规则全部数据类字段、`Source.note` 和
> `spec` 已进入 electrical revision，规则重排保持 revision 不变，供应字段仍只
> 改变 supply revision。独立复跑结果：KiCad 10.0.6、280 tests OK（0 skip）、
> T06 验收探针 26/26、真实生成 ERC 0 错/0 警告且 10 网络/17 器件逐引脚一致。
> 下文两个 P1 章节保留为历史问题与修复依据；当前待办从 T07 开始。

复核日期：2026-09-26  
复核范围：当前未提交工作区、`docs/HANDOFF.md` 声明的 T00–T06、KiCad 真实链路；T07/T08 只核对是否确实尚未实施。

## 结论

T00–T06 的主体实现可用，尤其是 IR 解码、连接预检、供电来源、器件子图规则、KiCad ERC 结构化解析、网表逐引脚对账以及 T06 固定夹具。交接报告对 T07/T08 尚未开始的说明属实。

当前版本不能作为首轮 T00–T08 的最终交付，也不应合并成一个声称“生产链路已完成”的提交。复核发现两个需要先修的实现问题：目录自校验没有进入生产生成路径；目录 revision 没有覆盖会改变电气结论的规则和来源说明。另有几项交接报告已经声明、但仍会影响自动化可信度的 T07/T08 阻塞项。

## 可复现结果

使用交接报告指定的 KiCad：

```powershell
$env:COS_KICAD_CLI = 'C:\Users\x\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe'
python -m unittest discover -s tests -t .
python gen.py examples/buck_12v_to_3v3.json -o output/review-deepseek-kicad
```

结果：

- KiCad 版本：10.0.6。
- 单元与集成测试：260 项，全部通过，0 跳过。
- 示例生成：ERC 0 错误、0 警告；10 个网络、17 个器件；网表与 IR 逐引脚一致；退出码 0。
- ERC 明确报告四项项目级禁用检查：`single_global_label`、`four_way_junction`、`simulation_model_issue`、`footprint_filter`。现有结论不覆盖这些检查。
- 未配置 KiCad 时，当前 `gen.py` 会跳过 ERC/网表核验并返回 0。这是 T07 尚未完成造成的已知行为。

## 必须修复的问题

### P1：目录自校验没有进入生产路径

`CatalogSnapshot.validate()` 实现了引脚、来源和封装焊盘映射检查，但 `ir.validate.validate()` 与 `ir.resolved.resolve()` 都只调用 `preflight()`，没有调用目录自校验。生成器经 `write_schematic()` 走 `resolve()`，因此测试里验证过的目录规则并没有成为生成闸门。

复现：把 MP1584EN 的 EP 物理脚号从 `9` 改成 `10`，再把该目录传给当前 `validate()` 或生成器。生产路径仍可通过并生成包含 `(pin "10" ...)` 的原理图。这个结果与 `docs/POWER_EXAMPLE_REVIEW.md` 中“会直接报 E-CAT-002，不会静默通过”的声明不一致。

同时，`CatalogSnapshot._check_pads()` 捕获所有读取器异常并降级成 `W-CAT-001`。`tests/test_device_facts.py` 又试图在 `snap.validate()` 外层捕获 `FootprintError` 并跳过；这个异常永远不会逃出 `validate()`。所以没有 KiCad 封装库的环境会出现一项失败，而不是预期的跳过或明确的严格模式失败。

修复要求：

1. 给目录校验定义明确策略，例如 `CatalogValidationPolicy` 或最小的 `strict_footprints: bool`，避免用异常类型隐式决定行为。
2. 在生产解析阶段执行目录结构校验。至少要让 `CatalogSnapshot.validate()` 的 error 合并进 `ResolvedIR.diagnostics`。
3. `schematic` profile 中，使用了带封装的器件时必须执行实际焊盘映射检查；读取失败应是阻断诊断，而不是成功状态里的 warning。
4. `generate` profile 可允许缺少 KiCad 库，但必须把“封装未核对”放入 BuildReport 的 pending/unchecked，且 `passed_profile` 不得暗示已完成原理图验证。
5. 增加生产入口回归：注入 EP=10 的目录，断言 `validate`/`resolve`/build 都得到 `E-CAT-002`，并且不会写出被标记为通过的构建。
6. 修正无 KiCad 库时的测试，使测试结果稳定、意图与实际 API 一致。

相关位置：`parts/catalog.py:133`、`parts/catalog.py:220`、`parts/catalog.py:233`、`ir/resolved.py:101`、`ir/validate.py:31`、`tests/test_device_facts.py:120`。

### P1：catalog revision 漏掉影响电气结论的数据

`_part_electrical_dict()` 没有序列化 `Part.connection_rules`，`_source_dict()` 没有序列化 `Source.note`。实测对只含 MP1584EN 的注入快照删除全部 `connection_rules`，或修改来源的 `note`，revision 都保持 `8af37519f83d83af` 不变；当时完整内置目录的对应旧 revision 为 `c996b1b5dea49154`。

这会让两份行为不同、证据含义不同的目录得到相同 revision。T07 如果把这个 revision 写进 BuildReport，报告无法证明实际采用了哪套电气规则。

修复要求：

1. 把所有会改变校验、生成、选型和证据解释的字段纳入 canonical electrical payload，至少包括 `connection_rules` 与 `Source.note`。
2. 为嵌套规则建立稳定序列化，明确集合排序规则，避免对象顺序造成非确定 hash。
3. 增加变形测试：逐个改变 connection rule 的 id/kind/pins/parameters、来源 note、pin、rating、footprint，revision 必须变化；只改变供应链字段时只允许 supply revision 变化。
4. 在 BuildReport 中同时记录 revision 和可读的 catalog 名称/版本，不把短 hash 当作唯一审计证据。

相关位置：`parts/catalog.py:54`、`parts/catalog.py:59`、`parts/catalog.py:71`。

## T07/T08 必须收口的已知阻塞项

这些不是交接报告隐瞒的问题，但在最终验收前必须完成：

1. `gen.py` 找不到 KiCad 时跳过 ERC 与网表并返回 0。T07 应按 profile 决定工具是否必需，并统一退出码。
2. 当前末尾输出“全部通过”，实际只表示 ERC 无错误且网表与 IR 一致；输出精度、效率、纹波、成本、PCB、热设计、仿真和实测并未通过。BuildReport 必须显示 verified/pending/failed 和验证范围。
3. 未登记 ERC warning 当前是 `W-ERC-002` 且不阻断。`schematic` profile 应明确其门禁策略，摘要必须显示 warning 数量，不能只写“全部通过”。
4. 产物仍直接写到指定目录，失败时可能留下工程文件；T07 应使用独立 build 目录和临时目录，验证完成后再发布最终产物。
5. README/DESIGN 仍保留旧测试数和旧网表口径；T08 更新后再对外发布。
6. 当前所有成果均未提交，且新增文件很多。修复后按任务边界拆分提交，防止一次巨型提交难以审计和回退。

## 建议优化

### P2：让诊断码和严重级别保持一致

`Diagnostic.__post_init__()` 只检查 code 已登记、severity 是合法字符串，没有约束 `E-*` 必须是 error、`W-*` 必须是 warning。当前静态扫描未发现已有误用，但构造 `Diagnostic("E-IR-STRUCT-001", "warning", ...)` 会成功，且 `is_error` 为 false。建议在构造时强制前缀与严重级别一致，并加一项遍历注册表/调用点的测试。

相关位置：`diagnostics.py:126`、`diagnostics.py:139`、`diagnostics.py:148`。

### P2：保留迁移约束的开闭区间语义

v0.1 迁移目前把 `>` 与 `>=` 都折叠为 `kind=min`，把 `<` 与 `<=` 都折叠为 `kind=max`。虽然 `source` 保留了原文，但未来目标判定如果只看结构化字段，会把严格不等式当成含等号边界。建议在 Target 中加入 `inclusive` 或规范化的 `operator`，并为边界值增加迁移与判定测试。

相关位置：`ir/migrate.py:33`、`ir/schema.py:127`。

## 对 DeepSeek 的下一轮执行指令

按以下顺序实施，不要直接从 T07 开始：

1. 建立修复前基线：保存当前 260 项测试和真实 KiCad 生成输出。
2. 新增失败测试，分别覆盖“损坏目录仍进入生产生成路径”和“电气规则变化但 revision 不变”。先确认测试在旧代码上失败。
3. 修复目录校验的生产接入与严格/宽松策略；不得在 `ir.validate`、`resolve`、CLI 各复制一套规则。
4. 修复 revision 的 canonical payload，并补充确定性和字段敏感性测试。
5. 完成 T07：`pipeline/models.py`、`pipeline/build.py`、`pipeline/workspace.py`；实现 profile、原子构建目录、统一退出码、机器可读 BuildReport 和范围准确的 CLI 摘要。
6. BuildReport 至少记录：输入 hash、catalog electrical/supply revision、KiCad 版本、profile、各阶段状态、全部 diagnostics、ERC ignored checks、netlist 统计、verified targets、pending targets、产物路径与 hash。
7. `passed_profile` 只能在该 profile 的必需阶段全部成功且无阻断诊断时为 true；找不到必需工具、目录未按 profile 完成核对、ERC 报告不可用、网表导出/解析/对账失败都必须非零退出。
8. 完成 T08 文档和全量回归。README/DESIGN、交接报告、测试数、示例网表数和 CLI 文案必须与同一次最终运行一致。
9. 最终提交证据应包含：无 KiCad 环境下的 generate profile、KiCad 10.0.6 下的 schematic profile、两个新增反例、260 项以上全量测试、真实示例报告及产物 hash。

验收命令至少包括：

```powershell
python -m unittest discover -s tests -t .
python -m checks.run_probes
$env:COS_KICAD_CLI = 'C:\Users\x\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe'
python gen.py examples/buck_12v_to_3v3.json -o output/final-review
```

另外增加自动断言，不靠人工读 stdout：

- BuildReport 可按 schema 解析。
- schematic profile 的 `passed_profile` 为 true 时，catalog/resolve/generate/ERC/netlist 五阶段均为 pass。
- 四个性能/成本目标没有仿真、实测或报价证据时保持 pending。
- 修改 EP 脚号、删除 connection rule、篡改网表、删除 ERC 报告、隐藏 KiCad CLI 都能得到预期的失败或降级状态。
