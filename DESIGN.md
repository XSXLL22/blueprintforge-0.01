# DESIGN —— 架构设计书

## 0. 一句话

用户提需求 → Claude 出 IR → 确定性后端出板文件 → 逐级验证 → 制造文件。
LLM 是**架构师**（选型、拓扑、参数、写理由），确定性代码是**执行者与验尸官**。

## 1. 两条铁律

1. **LLM 永不直接读写工程文件**（`.kicad_sch` / `.kicad_pcb` / Gerber）。
   它只碰 IR 与结构化诊断。类比编译器：LLM 是前端，生成器/校验器是后端。
2. **规则进代码，不进提示词。** 任何可确定性判断的事写成校验器；提示词里
   只保留「决策流程与知识」，不重复规则。长提示词是长文本失误的温床。

## 2. 上下文分层与 token 预算

| 层 | 内容 | 大小 | 生命周期 |
|----|------|------|----------|
| L0 常驻 | power-design skill、IR 契约（本文件第 3 节）、1 个干净示例 | ~3k token | 会话级，缓存 |
| L1 按需 | 器件库条目（每件一条：参数/封装/价格/生命周期） | ~50–100 token/件 | 选型时查询，用完即弃 |
| L2 永不入上下文 | 板文件、DRC 全文、仿真日志全文 | 数万 token | 只进代码，不进 LLM |

单次设计预算：输入 ~3k（L0 + 选中器件条目）+ 输出 ~1.5k（IR）+ 校验 0 +
修复 ≤3 轮。对比「LLM 直接写板文件」：单文件 50k+ token 且错误率高。

## 3. IR 契约（ir/schema.py）

版本号 `schema_version`，只增不删字段，规则收紧不动结构。当前 v0.1 只支持
电源模块（`power.buck` / `power.ldo`）。

| 字段 | 含义 | 约束 |
|------|------|------|
| project | 工程名 | 字母数字下划线 |
| module_type / topology | 模块类型与拓扑 | 枚举，且拓扑须与类型匹配 |
| electrical.vin / vout | min/typ/max（V） | 0 < min ≤ typ ≤ max；vout.max < vin.min |
| electrical.iout_max | 最大输出电流（A） | > 0 |
| components[] | ref 位号 / part 器件库型号 / role 角色 / value 参数 / footprint / rating | part 必须在器件库；role 与器件类别一致；regulator 恰 1 个 |
| nets[] | name / pins[]（ref+pin）/ netclass | 名称唯一；GND 必须存在；pin 引用必须真实 |
| constraints | 设计目标（效率/纹波/成本） | 自由 dict，校验器按已知键扩展 |
| design_rationale | 为什么选这个拓扑/器件 | 必填（缺了是警告） |
| assumptions / risks | 假设清单 / 风险标记 | 建议填写（缺了是警告） |

**引脚名约定**：IC 引脚按数据手册名（如 MP1584EN 的 `VIN`/`SW`/`FB`/`BST`/
`COMP`/`EN`/`GND`/`8`）；两脚无极性器件约定 `1`/`2`；二极管 `A`/`K`；
接插件 `1`/`2`。后端生成器持有一份「器件库 → 封装引脚表」做映射
（继承 BlueprintForge `cells.py` 的职能，但数据按型号进器件库，不硬编码）。

## 4. 验证链（逐级门禁，每级输出稳定错误码）

```
M0  IR 校验（纯代码，0 token）       E/W-IR-*
M2  kicad-cli DRC/ERC（0 token）     违规行文本 → 分类
M4  ngspice 仿真（0 token）          纹波/效率/负载跳变 → 结构化指标
M5  打样实测（¥ + 时间）             示波器/电子负载数据 → 结构化回灌
```

上一级不过，不进下一级。这是 BlueprintForge `PcbResult.ok` 哲学的延续：
**任一环节失败即整体 NG，不假装成功。**

## 5. 错误分类与修复循环

校验器输出 `Diagnostic(code, severity, path, message)`。LLM 只收到
`错误码 + 字段路径 + 一句话`，定向修 IR 后重跑——像修编译器报错，不像
读整份日志。修复轮次硬上限 3（继承 `max_fix_rounds` 惯例），超限即显式
报告失败与原因，留给人工。

错误码前缀：`E-` 拦路 / `W-` 可见不拦路；`-IR-STRUCT-` 结构、`-IR-ELECT-`
电气约束、`-IR-PARTS-` 器件与市场、`-IR-NETS-` 网络、`-IR-DOC-` 文档完整性。

## 6. 与 KiCad MCP 的分工

MCP 工具集本质是**验证/门禁层**（DRC/ERC/摆放评分/发布门禁），没有画图布线
能力——生成只能走文件层。所以：

- 生成：`backend/kicad` 写 S-expr 文件（0 token，可复现，可测试）；
- 主力验证：`kicad-cli`（0 token）；
- MCP：布线后抽查 `pcb_critique_placement`、发布前跑 `project_quality_gate`
  这类需要结构化诊断的环节；按需在子代理里加载，避免 67k token 常驻主上下文。

## 7. 器件库与供应链（parts/partsdb.py）

字段：`name / category / package / spec / lifecycle / price_cny / stock /
lcsc / alternatives`。v0.1 是 stub（价格为占位值，禁止用于真实采购决策）。
接入立创/DigiKey API 时只换数据来源，字段不变——LLM 与校验器看到的永远是
同一张表。`lifecycle=eol` 是硬错误（不得推荐停产料），`nrfnd` 是警告。

## 8. 目录结构

```
E:\deepseek\9-10\
├── README.md           愿景与路线
├── DESIGN.md           本文件：铁律/契约/预算/分工
├── ir/                 IR 数据模型与校验器
├── parts/              器件库（stub → API）
├── backend/kicad/      IR → KiCad 文件生成器（M1 起）
│   └── symbols/        vendor 官方符号（KiCad 10.0.6，CC-BY-SA-4.0）
├── examples/           示例 IR（干净模板，回归测试锚点）
├── output/             生成物（gitignore，gen.py 重新生成）
├── gen.py              前端：IR → 工程文件 → ERC/网表核验
└── tests/              纯标准库 unittest，零依赖
```

## 9. M1 实战教训（kicad-cli 行为，全部实测）

这些是拿真实 kicad-cli 10.0.6 撞出来的结论，M2（PCB 侧）大概率同样适用：

1. **符号只认内嵌，不认表。** kicad-cli 解析符号定义只读 `.kicad_sch` 的
   `(lib_symbols)` 内嵌副本，sym-lib-table 写了也不查（绝对路径、环境变量、
   `${KIPRJMOD}` 全试过）。所以官方符号 vendor 进仓库（`backend/kicad/symbols/`，
   见其 README），渲染时整体内嵌，输出自包含。
2. **网表导出不含电源符号引脚。** KiCad 10 的 `export netlist` 里 GND 符号/
   PWR_FLAG 的 `#PWR` 引脚**一律不出现**（连 KiCad GUI 自建的工程也一样）——
   不是 bug，是特性。电源网连通性只能靠 ERC 验证，网表对账只看器件引脚。
3. **导线重叠即合并网络。** 两个元件同一列、残端导线互相重叠 2.54mm，
   不同网络就被静默合并（GND+VIN 短路、网表里 VIN 消失）。布局间距必须用
   「reach 制」：元件中心距 ≥ 两元件 v_reach 之和 + GRID，v_reach 是
   引脚锚点 + 残端方向的最远电气延伸。
4. **PWR_FLAG 不合并网络。** 它的引脚名是空串，只能靠导线接触合入所在网；
   每个电源引脚各放一个 PWR_FLAG，多引脚电源网会被拆成 N 个单引脚网。
   合并要靠「电源符号」：`(power global)` + power_in 引脚 + **符号 Value**，
   KiCad 7+ 按 Value 自动并入全局网（GND 就是这么工作的）。
   所以对每个非 GND 电源网**现场生成**同名电源符号 `circuitos:PWR_<net>`
   （模板抄官方 GND），残端各放一个实例，网络即自动合并。
5. **驱动标记用「岛」不用逐个放。** 只有 IR 引脚里没有 power_out 的网络
   才需要 PWR_FLAG（告诉 ERC 该网有驱动）；有 power_out 引脚的网络再放
   PWR_FLAG 会触发 power_out×power_out 冲突。做法：图纸顶部给每个待标记
   网络画一条短导线 + PWR_FLAG + 该网电源符号（「PWR_FLAG 岛」）。
6. **「circuitos 库不在配置中」警告**：有 `.kicad_pro` + `sym-lib-table`
   （登记 `${KIPRJMOD}/circuitos.kicad_sym`）+ 库文件三件套即消除；
   内嵌符号的解析不受影响。库文件与内嵌定义同源，由生成器一并写出。

## 10. 路线图与下一步计划（2026-09-11 交接）

### M1 ✅ 已完成（本里程碑收尾状态）

- `backend/kicad/schematic.py`：方框 IC（内嵌自绘）+ 官方符号（vendor 内嵌）
  + 生成的电源符号，三种网络三种连接机制（标签/GND 符号/PWR_<net> 符号）；
- 输出：`output/buck_12v_to_3v3.kicad_sch` ERC **0 错误 0 警告**，网表 7 张网
  与 IR 逐网逐引脚一致；配套 `circuitos.kicad_sym` + `sym-lib-table`；
- 测试：34/34 通过（结构 14 + kicad-cli 集成 20，含网表对账）。

### M2 —— PCB 布局布线 + DRC 闭环

1. 移植 BlueprintForge `hdc/pcb/{layout,router,footprints}.py` 的布局布线；
2. **注意**：PCB 侧大概率同样「只认内嵌封装」——先做 M1 式的最小探测
   （一个元件 + 内嵌封装 → kicad-cli DRC）再动工，别默认 fp-lib-table 有效；
3. `.kicad_pcb` 生成器（footprint 从 partsdb 的 package 字段解析或 vendor
   KiCad 官方封装库，`pcb_get_board_summary` 类工具不参与生成）；
4. 验证门禁：`kicad-cli pcb drc` 0 违规 + 网络对账（与 M1 网表对账同思路）；
5. 输出：buck 例子的双层板，四角 M3 安装孔（给打样留）。

### M3 —— 错误分类修复循环 + power-design skill

1. 把 `ir/validate.py` 的错误码接进 LLM 循环：错误码 + 字段路径 + 一句话 →
   定向修 IR → 重跑，≤3 轮（继承 BlueprintForge `max_fix_rounds`）；
2. 重写 power-design skill（L0 常驻）：IR 契约、选型决策流程、错误码速查；
3. 验收：故意投喂坏 IR（器件不存在/网络悬空/拓扑不符），LLM 从错误码
   自修复到通过，全程不碰工程文件。

### M4 —— ngspice 仿真验证

1. `backend/spice/`：IR → 网表（ngspice 格式），buck 用 MP1584EN 的
   SPICE 模型（或行为级开关模型）；LDO 用标准件模型；
2. 指标：纹波、效率、负载跳变（与 IR constraints 对账）；
3. 输出结构化指标（数值 + 通过/不通过），不回灌原文日志。

### M5 —— 供应链 API + 打样实测

1. partsdb stub → 立创/LCSC API（价格/库存/生命周期），字段不变只换数据源；
2. 保留「价格为占位值、禁止真实采购决策」的护栏直至 API 接通；
3. 打样：M2 的板子下单，实测纹波/效率/温升，数据结构化回灌，闭环修 IR。

### 每次开工的固定顺序（防注意力漂移）

1. 先跑 `python -m unittest discover -s tests` 确认基线全绿；
2. 只做当前里程碑的一件事，完成即写测试 + 跑全量；
3. 不顺手改无关文件；调试文件一律放 `output/` 并随手清理。

