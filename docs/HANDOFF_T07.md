# T07 交接：构建流程、profile、BuildReport

日期：2026-09-26。执行者：Codex。T07 已实现并验证；T08 尚未完成。当前工作区保留此前 DeepSeek 的未提交成果，本轮未 commit/push。

## 交付内容

- `pipeline/models.py`：报告版本 1.0、五种状态、八个阶段与 profile 必需阶段、统一 passed_profile 判据。
- `pipeline/build.py`：严格解码、工具版本门禁、目录/设计检查、生成、ERC、网表对账、报告编排；每次构建只生成一份 ResolvedIR。
- `pipeline/workspace.py`：随机 build ID、独立 staging、报告与 manifest、目录改名发布；成功/失败产物分离。
- `gen.py`：参数解析和终端摘要，提供 auto/generate/schematic。没有业务规则副本。
- `ir/validate.py::validate_resolved`、`backend/kicad/schematic.py::write_resolved`：直接消费已解析快照；旧 validate/write_schematic 调用仍可用。
- 排版 `_plan` 使用快照的供电标记，不再暗中再次解析；`render_resolved` 本身也检查错误闸门。
- `tests/test_pipeline.py`：21 项流程测试；既有真实网表入口测试按新退出码/新 mock 边界适配。
- `tests/t06_acceptance.py`：从历史 T06 探针迁移，保留原负例，适配 T07 profile/退出码并额外断言半截报告真正走到 ERC 解析器。
- README、DeepSeek 实施指南/开工提示词、HANDOFF 入口、EXECUTION_LOG 更新。DESIGN 顶部标注新架构，历史正文留给 T08 整理。

## 固定契约

### Profile

| profile | 必需阶段 | 额外说明 |
|---|---|---|
| generate | input/catalog/resolve/generate/publish | 无 KiCad 可以成功；ERC/netlist 为 skipped；目录未核对项保留 |
| schematic | generate 全部 + tool/erc/netlist | 缺工具、版本不支持、封装不可读均阻断，不自动降级 |
| auto | 按 CLI 存在性选择上述之一 | 默认仅便于旧交互用法；报告记录 requested_profile 和实际 profile，CI 不使用 |

严格模式仅支持 **KiCad 10.0.6**（当前真实证据支持的版本），其他版本为 unsupported + 退出3。规则为可扩展白名单；扩版本先补夹具和真实验收。

五种阶段状态：pass/fail/skipped/unsupported/error。未请求阶段写 profile_not_requested；前置失败后写 prerequisite_failed。pending_targets 不是阶段成功条件；它保留尚未实现的用户指标，不能静默删除。

passed_profile 要求该 profile 全部必需阶段 pass、exit_code=0 且没有 error 诊断。schematic 中未登记 ERC warning（W-ERC-002）与重要检查被禁用（W-ERC-003）额外产生 E-BUILD-005，退出2。已登记豁免 W-ERC-001 可保留；所有 ignored_checks 写入报告并显示。

### 退出码

0=profile通过；1=输入结构/目录/设计失败；2=ERC/有效网表对账等产物核验失败；3=工具/版本/超时/报告不可用或严格封装未核对；4=I/O/内部故障。CLI 参数语法错误沿用 argparse 2，无构建报告。

相较 T06：结构错误 3→1；工具/报告错误 2→3；网表不一致 4→2；空/畸形网表属于报告不可用，退出3。没有为了保持旧探针数字而降低新门禁。

### 文件与报告

-o 是构建根目录。`.staging-<uuid>/` 内完成后整目录改名为 `passed-<uuid>/` 或 `failed-<uuid>/`。failed 中的图纸/ERC/网表仅供诊断，manifest.disposition=diagnostic_only。不会覆盖根目录旧工程，也不会重用旧构建的网表文件。

每次包含 build-report.json、manifest.json；成功/失败尽可能保留原输入、规范化 IR、目录电气快照和已生成产物。输出根目录不可写时 stderr 输出完整报告；发布失败时保留 staging 故障报告（若还能写），无 passed 目录。没有自动清理历史结果。

BuildReport 1.0 字段：

- build_id、started_at、finished_at、requested_profile、profile、exit_code、passed_profile、scope、report_path。
- stages：每个阶段的 status/reason/details；ERC details 含错误/警告数量、ignored_checks、版本，netlist details 含网络/器件数。
- diagnostics：全部实际产生的结构化诊断，不仅最后一条；catalog/resolve 去掉重复收集。
- input：原文件绝对路径、原始字节 SHA-256、规范化 SHA-256。规范化 JSON 排序对象键，保留数组顺序（部件顺序影响图纸布局）。
- catalog：name、id、electrical revision、supply_revision。当前 builtin@9a6bc694906b648b；供应 revision=490c0ddd707e7aad。
- provenance：源码/规则/vendor 符号/豁免清单逐文件哈希及整体指纹。可区分未提交源码状态，不能仅依赖 git HEAD。
- tools：实际 CLI 路径、查询所得版本、支持版本清单；generate 不启动 KiCad。
- migrations、pending_targets、verified_targets、unverified。四个示例目标原值/工况/source 全部保留，目前 verified_targets 为空。
- artifacts：以构建目录为基准的相对路径、SHA-256、字节数。报告自身由 manifest 哈希，manifest 不哈希自身。

原始 ERC/XML 的内部 source 字段可能保留执行时的 staging 路径；原报告字节不重写，以保存工具原始证据。最终产物定位以 report.artifacts 和 manifest 的相对路径为准。

## 验证与证据

证据根目录：`output/t07-evidence/`。output 被 gitignore 排除；测试与可复跑探针在 tests/，交接文档在 docs/。重要证据若需异机传递，需单独打包该目录。

- `tests-kicad.txt`：全量 **301 tests OK，0 skip**（280+21），KiCad 10.0.6。
- `tests-no-kicad.txt`：全量 **301 tests OK，35 skipped**；这35项没有算作真实集成通过。
- `final-affected-tests.txt`：最后一次排版共享快照改动后的 **40 tests OK，0 skip**。
- `t06-acceptance.txt`：迁移后的 T06 五节探针；原26项加1条“半截报告真正到达解析器”的断言，**27/27通过**。
- `final-real.txt`：最终显式 schematic 构建，BuildReport 路径在末行；实际报告和 manifest 在 `final-real/passed-<id>/`。
- `no-kicad-generate.txt` / `no-kicad-schematic.txt`：把 CLI 指到不存在的文件后，generate 退出0，schematic 退出3。

验证重点：EP=10 阻断且无图纸；严格模式无法通过注入宽松 policy 绕过封装；缺工具不能自动降级；未知版本拒绝；超时报告错误；ERC错误/未豁免warning/重要检查禁用阻断；旧网表不能复用；输出不可写与目录改名失败非零退出；重复构建不覆盖且原理图字节稳定；manifest所有哈希与报告内容一致。

## 可复制运行命令

```powershell
python -m unittest discover -s tests -t .
$env:COS_KICAD_CLI = 'C:\Users\x\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe'
python -m tests.t06_acceptance
python gen.py examples/buck_12v_to_3v3.json --profile schematic -o output/t08-check
python gen.py examples/buck_12v_to_3v3.json --profile generate -o output/t08-check
```

程序调用：`pipeline.build.build(Path(input), Path(output_root), profile='schematic', parts=...)` 返回 BuildReport；测试可以注入 CatalogValidationPolicy 的读取器，但 schematic 总会把 require_pads 强制为 True。生产外部调用不要直接拼接 Stage 或绕开 build()。

## 未完成和 DeepSeek 下一步

1. 做 T08 独立复核：检查新 profile/报告契约与负例，重跑测试/真实 KiCad，并审查真实图纸关键电气连接及注释布局。T07 没有做 GUI 图纸视觉验收。
2. DESIGN 正文中的旧 v0.1/M1 设计需要逐节整理；顶部已经标记过时，不要按旧设计撤销门禁。
3. 目前 schematic 是原理图软件检查范围；性能/成本全部 pending；没有仿真、PCB/DRC、热设计或实测。±2% 精度要求仍存在，而且先前电气审查指出无法被当前方案保证。
4. 当前未核对 C3/C4 两端电压/耐压；四条 ERC 被禁用检查明确记录。不要把 pending 清空来获得“全部通过”。
5. 原 P2（诊断码/严重级别一致性、旧约束开闭区间）没有混入 T07；下一轮可按反例修复。目录目前检查默认封装；允许覆盖的有效封装是否也需要逐实例实际 pad 核对，应在 T08 独立审查中验证范围。
6. 尚未有新 git 提交；保护工作区，不自动 commit/push。不自动进入 T09 或扩到 PCB。
7. 旧审查报告中的 `python -m checks.run_probes` 并不存在；当前正确探针命令是 `python -m tests.t06_acceptance`。历史审查留作记录，不按不存在的命令判实现失败。
