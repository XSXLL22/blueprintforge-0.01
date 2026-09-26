# CircuitOS 架构设计

CircuitOS 把结构化电路 IR 转换成可复现的 KiCad 原理图，并用确定性规则、ERC
和网表对账说明验证范围。当前实现停在原理图阶段；PCB、仿真、热设计、成本核验
和实物测试仍是后续工作。

## 设计原则

1. LLM 或其他上游只产生 IR，不直接编辑 KiCad 工程文件。
2. 能确定判断的规则写入代码，诊断使用稳定错误码和字段路径。
3. 缺工具、报告损坏、检查未运行和指标未验证都必须显式呈现。
4. 一次构建使用同一份 `ResolvedIR`，校验、渲染和网表对账不能各自解释输入。
5. 成功只覆盖请求的 profile；原理图通过不代表性能、PCB 或制造通过。

这些原则与仓库根目录的 [AGENTS.md](AGENTS.md) 一致。

## 数据流

```text
IR JSON
  │
  ├─ ir.decode                 严格解析、v0.1 → v0.2 显式迁移
  │
  ├─ ir.resolved.resolve       目录快照、连接、供电、子图和器件事实
  │           │
  │           ├─ ir.validate_resolved
  │           ├─ backend.kicad.write_resolved
  │           └─ backend.kicad.netlist.reconcile_resolved
  │
  ├─ backend.kicad.erc         KiCad ERC 报告解析、来源核对和豁免
  │
  └─ pipeline.build            profile 门禁、阶段状态、BuildReport、manifest
```

`gen.py` 只负责命令行参数和摘要。业务规则位于 `ir/`、`checks/`、`parts/`
和 `backend/kicad/`；`pipeline/` 负责顺序、失败传播和产物发布。

## IR v0.2

IR 定义在 `ir/schema.py`，入口解析在 `ir/decode.py`。解析器拒绝未知字段、错误
类型、重复 JSON key、NaN/Infinity、非法标识符及不支持版本。旧 v0.1 输入只在
`ir/migrate.py` 中转换，并在报告中保留迁移记录。

核心对象包括：

- `electrical`：输入/输出电压范围与最大输出电流；
- `components`：位号、目录器件、角色、值、封装覆盖和显式 NC；
- `nets`：网络、引脚引用与网络类别；
- `ports`：外部供电来源和方向；
- `targets`：带单位、方向、来源和验证工况的设计目标；
- `design_rationale`、`assumptions`、`risks`：设计依据与未决风险。

目前目标只被登记并保留，尚未实现性能求值器。示例中的输出精度、效率、纹波
和成本均进入 `pending_targets`，不能因为原理图通过而标记为满足。

## 目录和规则

`parts/partsdb.py` 提供器件事实；`CatalogSnapshot` 将电气数据和供应数据分别
哈希。电气 revision 覆盖引脚、额定值、封装、来源和连接规则；报价、库存等只
改变 supply revision。

目录校验负责：

- 记录结构、来源、引脚唯一性和允许封装；
- 符号引脚号与实际封装焊盘号一致；
- 严格模式无法读取封装时阻断，而不是降级成成功；
- generate 模式无法读取时写入 warning 和 `unverified`。

连接、供电来源、器件范围和外围子图分别在 `checks/` 下实现。MP1584EN 的 EN、
BST、FREQ 和 COMP 规则是目录数据，由共享检查器解释。

## KiCad 后端

`backend/kicad/schematic.py` 生成确定性的单页 `.kicad_sch`。官方符号的必要
子集保存在 `backend/kicad/symbols/`，以便无 KiCad 环境也能生成自包含原理图。
这些符号沿用 KiCad 库的 CC-BY-SA-4.0，详见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

KiCad 10.0.6 的实测约束包括：

- CLI 依赖原理图内嵌符号定义；
- 导出网表不包含电源辅助符号的普通器件节点；
- 电源网络使用全局电源符号合并；
- 只有已证明供电来源且没有 `power_out` 的网络才添加 PWR_FLAG 岛；
- 网表必须与 IR 双向、逐物理引脚对账，辅助符号只按明确的 `lib_id` 排除。

ERC 适配器检查 JSON 结构、报告新鲜度、原理图对应关系和 KiCad 版本。ERC error
不可豁免；warning 只能按规则和对象精确登记。严格 profile 中未登记 warning 或
重要检查被禁用都会阻断。

## 构建状态机

T07 定义八个阶段：

```text
input → tool → catalog → resolve → generate → erc → netlist → publish
```

每个阶段只有 `pass`、`fail`、`skipped`、`unsupported`、`error` 五种状态。
前置失败后续阶段写 `prerequisite_failed`，不能看起来像成功执行过。

| Profile | 必需阶段 | 能声明的结论 |
|---|---|---|
| `generate` | input、catalog、resolve、generate、publish | 只完成确定性生成 |
| `schematic` | generate 全部 + tool、erc、netlist | 原理图软件检查通过 |
| `auto` | 按 CLI 是否存在选择以上之一 | 仅供交互兼容，不用于 CI/发布 |

严格模式目前只接受已有真实集成证据的 KiCad 10.0.6。扩展版本支持必须先增加
真实报告夹具和集成验收。

## 输出与可追溯性

每次构建先写入 `.staging-<build-id>`，结束后整体发布为 `passed-<build-id>` 或
`failed-<build-id>`。失败目录是诊断材料，不是可交付工程。

`build-report.json` 记录输入哈希、规范化 IR 哈希、目录版本、工具版本、源码
指纹、阶段状态、全部诊断、待验证目标、未核对项和产物。`manifest.json` 记录
相对路径、长度和 SHA-256。报告带运行时间和随机 ID；生成的工程文件本身仍须
对相同输入保持确定。

退出码契约：

| 代码 | 含义 |
|---|---|
| 0 | 所请求 profile 通过 |
| 1 | 输入、目录或设计检查失败 |
| 2 | ERC 或有效网表的产物核验失败 |
| 3 | 工具、版本、超时或报告不可用 |
| 4 | I/O 或内部错误 |

## 仓库结构

```text
backend/kicad/  KiCad 生成、ERC、网表、封装及 S-expression 处理
checks/         连接、供电、器件和外围子图规则
docs/           当前交接、审查和历史执行记录
examples/       示例 IR
ir/             schema、严格解析、迁移、解析快照和校验
parts/          器件目录、数值和连接规则
pipeline/       profile、状态机、报告与构建目录
review/         早期缺陷复现资料，仅作历史证据
tests/          单元、负例、夹具和真实 KiCad 集成测试
```

## 验证策略

基础回归使用标准库 `unittest`，不要求第三方 Python 包。无 KiCad 环境仍运行全部
纯软件测试，真实工具测试明确 skip。GitHub Actions 运行无工具回归和 generate
profile；本地发布验收另用 KiCad 10.0.6 运行 schematic profile。

测试重点不是覆盖实现行数，而是证明失败不会被误读为成功：损坏目录、重复引脚、
缺必接脚、错误外围子图、畸形 ERC、工具超时、网表被篡改、旧产物复用和输出目录
故障都必须得到稳定的非零结论及报告。

## 当前边界和后续路线

已完成 T00–T07：严格 IR、目录事实、连接与供电检查、原理图生成、ERC、网表
对账和 BuildReport。T08 是独立最终复核与图纸视觉检查。

后续能力按证据逐步增加：

1. 完成 T08，审查图纸可读性并核对最终文档；
2. 建立带来源和确定性选参的审核模板；
3. PCB 生成、布局布线、DRC 和制造文件；
4. SPICE/计算验证纹波、效率和动态响应；
5. 供应链实时数据与实测回灌。

在对应阶段完成前，不应宣传自动 PCB、性能达标或可直接制造。
