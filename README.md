# CircuitOS（BlueprintForge）

[![CI](https://github.com/XSXLL22/blueprintforge-0.01/actions/workflows/ci.yml/badge.svg)](https://github.com/XSXLL22/blueprintforge-0.01/actions/workflows/ci.yml)

工程把结构化电路 IR 转成自包含 KiCad 原理图，并对输入、器件目录、连接、ERC 和导出网表进行确定性检查。自然语言设计、PCB、仿真和可制造交付属于后续路线，当前没有完成。

当前进度：T00–T07 完成，T08 最终独立验收待执行。IR 为 v0.2，支持显式迁移 v0.1。当前示例为 10 网络、17 器件。最新接口与交接见 [T07 交接](docs/HANDOFF_T07.md)。

## 使用

```powershell
# 无需安装 KiCad，使用仓库内嵌符号；只保证生成范围
python gen.py examples/buck_12v_to_3v3.json --profile generate -o output

# CI/验收必须显式指定；缺工具、封装库或未验证版本会失败
$env:COS_KICAD_CLI = 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
python gen.py examples/buck_12v_to_3v3.json --profile schematic -o output

python -m unittest discover -s tests -t .
python -m tests.t06_acceptance
```

纯 Python 标准库，无新增依赖。严格模式目前只接受已有真实验收证据的 KiCad **10.0.6**。其他版本返回 unsupported；扩展支持需补真实验证，不能只修改版本清单。

不写 `--profile` 时为 auto：找到 CLI 则选择 schematic，否则 generate。实际选择会进入报告并显示在终端。auto 不用于 CI/发布验收；找到但无法运行/不支持的 CLI 不会自动降级。

## 报告与输出

每次构建先在 `output/.staging-<id>/` 完成；成功后整个目录改名为 `output/passed-<id>/`。失败记录在 `output/failed-<id>/`，其中工程文件仅供诊断。输出不可写或改名失败返回 4，尽可能留下 staging 故障报告；无法写报告时完整 JSON 输出到 stderr。

- `build-report.json`：schema_version=1.0、请求和实际 profile、passed_profile、退出码、各阶段状态、全部诊断、pending_targets、unverified、输入/目录/工具/源码指纹、产物清单。
- `manifest.json`：相对路径、文件长度、SHA-256；包含报告本身，不包含 manifest 自身，避免循环哈希。
- `input.json`、`normalized-ir.json`、`catalog-electrical.json`：输入与目录审计副本。
- 原理图、符号库、工程文件和严格模式的 ERC/网表文件。

`passed_profile` 只表示请求范围的必需阶段均通过。即使 schematic 通过，输出精度、效率、纹波、成本四个目标仍在 pending；C3/C4 耐压未核对、ERC 禁用检查也会明确展示。示例的 ±2% 精度需求没有被降低，也未被证明能满足。

## 退出码和严格门禁

| 退出码 | 意义 |
|---|---|
| 0 | 所选 profile 的必需阶段通过 |
| 1 | 输入结构、器件目录或设计检查失败 |
| 2 | ERC 违规/未豁免告警/重要检查禁用，或有效网表对账不一致 |
| 3 | 工具缺失/版本不支持/超时、严格模式封装无法核对、ERC/网表报告不可用 |
| 4 | 输入/输出 I/O 或内部错误 |

参数语法错误保留 argparse 的退出码 2；此时尚未开始构建。ERC 已登记豁免的 warning 可保留；未登记 warning 和重要检查禁用会阻断 schematic。其余禁用检查全部记入报告，结论不覆盖它们。

## T07 迁移注意

`-o` 现在指定构建根目录，文件不再直接写在根下。工具失败原退出 2 改为 3；网表不一致原退出 4 改为 2；结构不合格原退出 3 改为 1。旧脚本必须按报告路径及新退出码适配。

历史 `output/t06-evidence/` 探针保留原契约，不覆写历史证据。当前验收入口为 `python -m tests.t06_acceptance`，结果写入新的 `output/t07-evidence/t06-acceptance-<id>/`。

架构：`gen.py → pipeline.build → decode → resolve/validate_resolved → write_resolved → ERC → reconcile_resolved → report/manifest`。构建只解析一次，pipeline 不复制电气规则。

## 文档与许可

- [架构设计](DESIGN.md)
- [文档索引](docs/README.md)
- [贡献指南](CONTRIBUTING.md)
- [GitHub 上传清单](docs/GITHUB_UPLOAD.md)
- [当前 T07 交接](docs/HANDOFF_T07.md)
- [DeepSeek 后续实施指南](DEEPSEEK_IMPLEMENTATION_GUIDE.md)

项目代码采用 [MIT License](LICENSE)。`backend/kicad/symbols/` 中的 KiCad 官方
符号摘录采用 CC BY-SA 4.0，详见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。
