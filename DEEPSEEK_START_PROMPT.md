# 复制给 DeepSeek 的接手指令（T07 之后）

你接手 CircuitOS / BlueprintForge。先阅读 `docs/HANDOFF_T07.md`、`docs/HANDOFF.md` 顶部、`DEEPSEEK_IMPLEMENTATION_GUIDE.md`、`docs/EXECUTION_LOG.md` 的 T07 记录以及 README。

当前 T00–T07 已实现；两个 P1 已关闭。你的任务是独立审查 T07 并完成 T08，不能重新从 T00 开始，也不能把测试全绿当作 PCB/性能达标。

1. 记录 git 状态并保护现有未提交修改；不自动 commit/push，不覆写 review/ 和历史 output/ 证据。
2. 审查 `pipeline/models.py`、`pipeline/build.py`、`pipeline/workspace.py`、薄入口 `gen.py`。核查所有失败能否得到非零退出和真实阶段状态，失败产物是否只能作为诊断材料。
3. 执行 `python -m unittest discover -s tests -t .`，然后在 KiCad 10.0.6 下执行 `python -m tests.t06_acceptance` 和显式 `--profile schematic` 生成。缺工具时明确保留真实验收未完成；不得把 skipped 计作实际通过。
4. 用不存在的 COS_KICAD_CLI 路径分别跑 generate/schematic：前者可以生成、ERC/netlist 必须 skipped；后者必须退出 3，不能降级。
5. 检查 build-report.json 与 manifest.json：逐文件核对 SHA-256；检查八个阶段、catalog id/revision/supply_revision、工具版本、输入和源码指纹。仅当必需阶段全 pass 且无阻断诊断时 passed_profile 才能为 true。
6. 核查四个用户目标始终保留原数值/工况/source，并处于 pending；C3/C4 未验证耐压、ERC ignored_checks 不能消失。不要擅自修改 ±2%、2A、效率、纹波或成本要求。
7. 完成 T08 的真实 KiCad 图纸可视化检查：关键引脚与连线、EN 分压、BST–SW、FREQ、COMP 串联 RC、注释布局及可读性。T07 没有声称完成这项视觉验收。
8. 更新 DESIGN.md 剩余旧 M1/v0.1 文本和最终交付文档；历史记录保留日期与版本语境。
9. T07 的策略固定为：schematic 缺封装必失败；未登记 ERC warning/重要检查禁用阻断；只支持有真实证据的 10.0.6。若要扩版本，先补真实报告夹具与集成测试。
10. 检查剩余 P2：Diagnostic code/severity 一致性、旧目标迁移开闭区间、允许封装覆盖的实际 pad 验证范围。确认缺陷后给最小复现并修复，避免无依据扩展架构。

允许有依据调整实现，但不能删负例、降级门禁、让 stub 绕过版本/封装前置条件来获得假通过。统一的最终报告必须有：改变了什么、真实测试/跳过数、实跑命令、可点击证据、未完成事项和下一步。T08 验收完成后停止，不自动进入 T09/PCB/仿真，也不下单或操作硬件。
