# EXECUTION LOG —— CircuitOS 首轮（T00–T08）执行记录

格式遵循 `DEEPSEEK_IMPLEMENTATION_GUIDE.md` 第 11 节。每条记录都区分
**已实现 / 真实验证 / 仅 mock / 未运行 / 未解决**。

---

## T00：核实基线与可用环境

**任务**：T00 / 核实基线与可用环境
**日期**：2026-09-26

### 起点

- 提交：`4942bf8115031cf35043e953ceb39f1187cb864a`（M1：IR→KiCad 原理图生成器）
- 工作区未提交修改：仅有 4 个未跟踪文件——`DEEPSEEK_IMPLEMENTATION_GUIDE.md`、
  `DEEPSEEK_START_PROMPT.md`、`REVIEW_AND_PLAN.md`、`review/`（审查交付物）。
  业务源码无本地改动，未覆盖任何历史证据。
- 未发现 `AGENTS.md`。已读：README.md、DESIGN.md、实施指南、审查报告、
  `review/reproduce_findings.py`、`review/probe-results.json`、
  `review/baseline-tests.txt` 及 `ir/ parts/ backend/ gen.py tests/` 全部实现。

### 环境

| 项 | 值 |
|---|---|
| Python | 3.14.7（`C:\Python314\python.exe`） |
| kicad-cli | `C:\Users\x\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe` |
| KiCad 版本 | **10.0.6**（`kicad-cli version` 实测） |
| kicad-cli 命令能力 | `sch erc --format json|report`（含 `--exit-code-violations`）；`sch export netlist --format kicadsexpr|kicadxml|cadstar|orcadpcb2|spice|spicemodel|pads|allegro` |
| 原厂资料 | `output/review-evidence/MP1584.pdf`（Rev 1.0，8/8/2011，17 页）可用，本次逐页复核（第 1/2/3/4/9/10/11/12/13/14 页） |
| git 远端 | 未核验（本轮不推拉远端，按要求不覆盖工作区强制同步） |

### 基线运行结果（都是本次真跑，非引用历史）

| 命令 | 结果 | 证据 |
|---|---|---|
| `python -m unittest discover -s tests -v` | **34 tests, 34 pass, 0 fail, 0 skip**（本机有 KiCad，集成用例未跳过） | `output/t00-evidence/baseline-tests-rerun.txt` |
| `python review/reproduce_findings.py` | 14 探针，**全部复现审查报告的缺陷行为**（与 `review/probe-results.json` 逐条一致） | `output/t00-evidence/probe-rerun.json` |
| `python gen.py examples/buck_12v_to_3v3.json -o output/t00-evidence/gen-baseline` | 退出码 0，打印「全部通过」 | `output/t00-evidence/gen-baseline.txt` |

> 注：`review/` 下的历史审查证据**未改动**；本次复跑输出另存到 `output/t00-evidence/`，
> 两份可对比但不互相覆盖。

### 核实结论（与审查报告一致，均为本次复现）

1. **H1** MP1584 引脚表 8 个脚全部错位——原厂第 2 页 TOP VIEW 与第 4 页 PIN FUNCTIONS 为
   1 SW / 2 EN / 3 COMP / 4 FB / 5 GND / 6 FREQ / 7 VIN / 8 BST，且封装有散热焊盘。
2. **H2** 示例把 EN 直接接 VIN（10.8–13.2V），原厂第 2 页 ABSOLUTE MAXIMUM RATINGS
   "All Other Pins: -0.3V to +6V"，**超绝对最大值**。
3. **H3** 自举电容 C5 接 BST–GND，原厂第 4 页要求 BST–SW。
4. **H4** 器件表无 FREQ 引脚，示例无频率设定支路；补偿支路 R3(3.3k) 与 C6(10nF)
   并联对地，原厂第 12/13 页要求 COMP 对地的**串联 RC**（第 13 页 Table 3：
   3.3V/22µF/6.8–10µH → R3=68.1kΩ、C3=220pF、C6=None）。
5. **H5** MP1584 生命周期标 `active`，原厂产品页为 **NRFND**（不推荐新设计，但仍供现有客户）。
6. S1–S10 软件缺陷全部可复现（S3 生产入口不对账、S4 ERC 畸形报告假通过、
   S5 目录注入未贯穿、S6 封装覆盖失效、S7 NaN/Infinity 放行 + `project=123` 抛
   `AttributeError`、S8/S9/S10 能力缺口）。

### 环境限制（影响验收口径）

- **KiCad 集成验收本轮可以真跑**（10.0.6 已安装），不需要标"未完成"。
  但仍限定单一版本：其他主版本（7/8/9）未做兼容矩阵，代码只声明支持 10.0.x。
- 无实物测量、无 ngspice 模型 ⇒ 效率/纹波/温升等**性能目标本轮一律为未验证**。
- `parts/partsdb.py` 价格/库存仍是 stub ⇒ 成本目标不作为采购结论。

### 下一步

T01：修正器件事实（MP1584 引脚表、散热焊盘 pad 映射、生命周期）与示例电气设计
（EN 分压、BST–SW 自举、FREQ 设定、补偿支路），并把计算与来源写入
`docs/POWER_EXAMPLE_REVIEW.md`。

---

## T01：修正器件事实与示例电气设计

**任务**：T01 / 器件事实 + 示例电气设计
**日期**：2026-09-26

### 起点

- 上一任务 T00 完成，基线证据在 `output/t00-evidence/`。
- 待修问题：H1（引脚表全错位）、H2（EN 超绝对最大值）、H3（自举接错网络）、
  H4（缺 FREQ、补偿支路错）、H5（生命周期记错）。

### 实现

**器件目录（`parts/partsdb.py`）**

- MP1584EN 引脚表按原厂第 2/4 页改为 1 SW / 2 EN / 3 COMP / 4 FB / 5 GND /
  6 FREQ / 7 VIN / 8 BST / **9 EP**；删掉凭空的 PGND。
- 新增字段：`manufacturer` / `mpn` / `abs_max`（与 `operating` 分开）/
  `supported_topologies` / `connection_rules`（预留）/ `review_state`；
  `Source` 记录 URL + 版本 + 页码 + 复核日期；`Pin` 增加 `required` /
  `nc_condition`。
- 生命周期 active → **nrfnd**（附产品页来源），替代型号 MP2338 仅作备注。
- 新增数据手册用到的阻容：124k、40.2k、68.1k、100k、24.9k、200k（0603 1%）、
  220pF/50V C0G（0402）。
- **没有**把 SW 的绝对最大值（VIN+0.3V）写成常数——它随工况变，只记在来源里。

**新模块**

- `backend/kicad/sexpr.py`：正确的 s-expr 词法/语法解析（引号、转义、
  字符串内括号、嵌套、括号不平衡报错）。T05 的网表解析复用同一份。
- `backend/kicad/footprint.py`：读 `.kicad_mod` 的焊盘表，区分电气焊盘与
  无编号钢网焊盘/NPTH；返回 `pad_prop_heatsink` 等属性。
- `parts/values.py`：带单位数值解析（`49.9k`/`4R7`/`0.1µF`/`10uH`/`85%`/`10元`），
  解析失败返回 None 而**不猜**；`magnitudes_match` 三级比较。
- `parts/catalog.py`：`CatalogSnapshot`（不可变目录快照）+ 自校验：脚号唯一、
  引用无歧义、sourced 记录必须有来源与页码、**符号脚号 ↔ 封装电气焊盘集合
  必须相等**（读实际封装文件）。电气主数据与报价分开哈希。
- `diagnostics.py`：扩展诊断模型（`stage/object_id/rule_id/evidence/actual/
  expected`）+ **错误码登记表**；未登记的码直接抛错，防止码义漂移。

**示例（`examples/buck_12v_to_3v3.json`）**

外围参数整体采用原厂第 1 页典型应用 + 第 13 页 Table 3，未自行另算：

| 项 | 修正前 | 修正后 | 依据 |
|---|---|---|---|
| EN | 直接接 VIN（13.2V，超 6V 绝对最大值） | R5=100k/R6=24.9k 分压 → 2.63V，UVLO≈7.5V | 第 1 页、第 14 页 |
| 自举 | C 跨 BST–GND | C4=0.1µF 跨 **BST–SW** | 第 4 页 |
| 频率 | 无（器件表也没有 FREQ 脚） | R4=200k → 485kHz | 第 9 页公式 |
| 补偿 | R3(3.3k)∥C6(10nF) 对地 | R3=68.1k **串联** C3=220pF 对地 | 第 12/13 页 |
| FB 分压 | 49.9k/16k | 124k/40.2k | 第 1 页、第 10 页 |
| 网络数 | 7 | 10（新增 EN / FREQ / COMP_RC） | — |

需求（vin/vout/iout、±2%、效率、纹波、成本）**一字未改**。

**计算与来源**：全部写进 `docs/POWER_EXAMPLE_REVIEW.md`（逐项给页码、公式、
带入值、结果）。关键结论见下。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests` | **55 tests, 55 pass, 0 fail, 0 skip**（原 34 → 55，新增 21） | 本页下方说明 |
| 目录自校验 | `CatalogSnapshot.builtin().validate(pad_provider=electrical_pads)` | 0 错误 0 警告 | `output/t01-evidence/` |
| 反例：散热焊盘脚号写成 10 | 同上 | **E-CAT-002 拦截**（脚悬空 + 焊盘无网络两条） | 同上 |
| 真实生成 + ERC | `python gen.py examples/buck_12v_to_3v3.json -o output/t01-evidence/gen` | 退出码 0；**KiCad 10.0.6 真跑 ERC：0 错误 0 警告** | `output/t01-evidence/gen-run.txt`、`gen/buck_12v_to_3v3.erc.json` |
| 真实网表 | `kicad-cli sch export netlist --format kicadxml` | 10 网络，节点为物理引脚号，逐个对上第 2/4 页 | `output/t01-evidence/netlist-real.txt` |

真实网表（节选，证明 H2/H3/H4 已修）：

```
      /BST (1): C4.1 U1.8          ← 自举电容一端在 BST(8)
       /EN (4): R5.2 R6.1 U1.2     ← EN 走分压，不再直接接 VIN
     /FREQ (6): R4.1 U1.6          ← 频率设定支路存在
     /COMP (2): R3.1 U1.3          ← 补偿：COMP→R3→C3→GND 串联
  /COMP_RC (3): C3.1 R3.2
        SW (8): C4.2 D1.1 L1.1 U1.1   ← 自举电容另一端在 SW
       GND (7): ... R4.2 R6.2 U1.5 U1.9   ← 散热焊盘 U1.9 在 GND
```

### 关键发现：输出精度 ±2% **无法由 MP1584 保证**（不符合项，未改需求）

- 标称：0.8×(1+124k/40.2k) = **3.2677V**（−0.98%）。
- VFB 容差 ±3%（0.776/0.8/0.824V，第 3 页）叠加 ±1% 电阻：
  最坏 **3.122–3.417V（−5.4%/+3.6%）**，超出 3.234–3.366V 目标。
- 换成 0.1% 电阻也只能到 3.165–3.371V（−4.1%/+2.1%），**仍不达标**——
  瓶颈是 VFB，不是电阻。
- 处理：需求原样保留，结论写进示例的 `risks` 与复核文档第 3.2/5 节；
  候选方向（换基准更紧的器件 / 输出微调 / 放宽需求）**未实施**，
  其中放宽需求属于需求变更，须用户决定。

### 未完成 / 未验证

- T01 要求的四个**反例子图测试**（错误 EN、BST–GND、缺 FREQ、COMP 并联）
  在 **T03** 与统一连接检查器一起交付——它们需要"子图规则"检查能力，
  与 T03 的 `checks/connectivity.py` 是同一处代码，不重复实现。
- 效率/纹波/成本/结温**全部未验证**（无模型、无实测、无报价）。
- 目录中绝大多数料仍是泛化料号（`review_state="generic"`），未落到可采购
  MPN；C6（可选补偿电容）判据未用真实 ESR 核对。
- `gen.py` 仍会打印"全部通过"：当前它只表示 ERC 无错误，网表对账（S3）与
  报告状态（T07）尚未接入，**这句话现在仍不可信**，T05/T07 修。

### 下一步

T02：严格结构解析与数值约束（`ir/decode.py`、v0.2 迁移、结构化字段诊断），
消除 NaN/Infinity/布尔/未知键/重复 key 等输入路径。

---

## T02：严格结构解析与数值约束（IR v0.2）

**任务**：T02 / 严格结构解码 + 数值化设计目标 + v0.1 迁移
**日期**：2026-09-26

### 起点

- 上一任务 T01 完成，基线证据在 `output/t01-evidence/`；环境 KiCad 10.0.6。
- 待修问题（T00 复现，`review/probe-results.json`）：
  - **S7 输入卫生**：`nonfinite_current_NaN` / `nonfinite_current_Infinity` /
    `constraints_not_validated` 的观察结果都是 `{"ok": true, "codes": []}`
    ——非有限数与看不懂的约束被静默接受；`malformed_project_type` 直接
    `AttributeError: 'int' object has no attribute 'replace'`。
  - 输入层没有任何"结构不通过就不生成"的闸门。
- T02 只负责**结构与数值**（S7 族）。S9 族的器件/拓扑部分（评级、拓扑匹配、
  续流二极管）不属于本任务，见"未完成"。

### 实现

**新增模块**

| 文件 | 行数 | 职责 |
|---|---|---|
| `ir/decode.py` | 663 | 严格解码：结构 / 类型 / 语法。任何 error ⇒ `draft is None` ⇒ 不产任何文件 |
| `ir/names.py` | 129 | 命名规则的**唯一**来源（工程名/位号/网络名/引脚引用/端口名） |
| `ir/migrate.py` | 166 | v0.1 `constraints` → v0.2 `targets`，逐条留痕 |

**分层**：`decode.py`（结构）→ `schema.py`（数据模型/序列化）→
`validate.py`（语义）。三层共用同一套数据类，`from_dict` 与 `decode_ir`
共用同一条迁移路径——不存在"两个解析器、两套语义"。

**输入卫生的判据（每一条都能复现）**

| 输入 | 现在的结果 |
|---|---|
| `"iout_max": NaN`（字符串） | `E-IR-DECODE-003`（类型），并指出这是字符串冒充数值 |
| `"iout_max": NaN`（JSON 字面量） | `E-IR-DECODE-004`（非有限）；**理由**：NaN 参与的比较全为假，`0 < nan <= nan` 会"通过"下面每一条范围规则 |
| `1e400`（溢出成 inf） | `E-IR-DECODE-004`（错误信息保留原字面量 `1e400`，不显示成 `inf`） |
| `true` 当数值 | `E-IR-DECODE-003`（`bool` 是 `int` 子类，`float(True)=1.0` 正是缺陷本身） |
| `"2.0"` 当数值 | `E-IR-DECODE-003`（否则 `"2A"`/`"2.0 "` 的边界只能靠猜） |
| 重复 JSON key | `E-IR-DECODE-006`（`object_pairs_hook` 保留重复项） |
| 未登记的字段/键 | `E-IR-DECODE-005` / unknown target key 也报错，**不静默忽略** |
| `efficiency_min: 85`（想写 85%） | `E-IR-DECODE-011`（比值目标 >1.0） |
| `"project": 123` / 保留名 `con` | `E-IR-STRUCT-002`（结构化诊断，不再 `AttributeError`） |

**代码复用规则**：语义层已有同义错误码时，结构层**沿用**该码，不新造——
project→`E-IR-STRUCT-002`、module_type→`003`、topology→`004`、
role→`E-IR-PARTS-002`、网络名→`E-IR-NETS-003`。仅结构层独有的缺陷才用
`E-IR-DECODE-001…013`（码表在 `diagnostics.py`，未登记的码直接抛错）。

**防御性复核**：`validate()` 自己再查一遍有限性（`E-IR-DECODE-004`）与
比值 >1.0（`E-IR-DECODE-011`）。理由：`PowerIR` 可以被直接构造（工具、
测试、以后的其他前端），不能假设所有对象都过了 decode——这条由
`TestDefenseInDepth` 七个用例守住。

**迁移不是验证**：v0.1 → v0.2 的每一条换算都发 `W-IR-DECODE-002`
（"需人工确认"）并保留原文 `source`（如 `">85%"`）；单位写在文本里的
（`ripple_target_mv`）只乘一次；方向不符（`<` 写成 min 类）报错；
未知的 legacy 键报错而**不是丢掉**。迁移报错时 `from_dict` 也抛
`ValueError`——丢掉一个约束的下游后果比报错更糟：目标列表少了一项，
而"没有报错"会被读成"所有约束都满足"。

**示例（`examples/buck_12v_to_3v3.json`）**

- `constraints` → `targets`：效率 `0.85`（源 `>85%`）、纹波 `0.05`（源 `<50`）、
  成本 `10.0`（源 `<10`），逐项带 `source`。
- 新增 `vout_accuracy: 0.02`：±2% 是用户明写的需求，v0.1 时只藏在
  `electrical.vout` 的 min/max 里，没有作为目标登记——**需求不能只写进
  一个没有检查的地方**。测试断言它必须等于 `(vout.max−vout.typ)/vout.typ`。
- 目标条件（工况）是**本设计指定**的假设（12V/2A/25°C、纹波带宽 20MHz），
  原需求未约定——写进 `assumptions` 与 `conditions`。成本目标没有数量口径
  与计价货币，`conditions` 留空，按 §4.1 **不给通过章**（`W-IR-DOC-004`）。
- 需求本身（10.8–13.2V、3.234–3.366V、2A、>85%、<50mV、<10 元）**一字未改**。

**gen.py 闸门**：`decode` 不合格 ⇒ 打印诊断后以退出码 3 结束，
**不写任何工程文件**。退出码：0 成功 / 1 语义错误 / 2 ERC 有错 /
3 结构不合格（T07 统一到 pipeline/BuildReport）。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests` | **121 tests, OK, 0 fail, 0 skip**（T01: 55 → 121，新增 66） | 本页 |
| 真跑生成 + ERC | `python gen.py examples/buck_12v_to_3v3.json -o output/t02-evidence/gen` | 退出码 0；**KiCad 10.0.6：0 错误 0 警告** | `output/t02-evidence/gen-run.txt` |
| ERC 原始报告 | `kicad-cli sch erc --format json -o output/t02-evidence/erc-real.json …` | `sheets[0].violations` 长度 **0** | `output/t02-evidence/erc-real.json` |
| 网表回归 | `kicad-cli sch export netlist --format kicadxml` | **10 网络 / 17 器件，与 T01 逐网络逐引脚完全一致** | `output/t02-evidence/netlist-real.xml` |
| 结构闸门 | `python gen.py output/t02-evidence/bad-ir.json -o …/gen-bad` | 退出码 **3**，`gen-bad/` **未创建**，报 `E-IR-DECODE-003` + `E-IR-DECODE-011` | `output/t02-evidence/gen-bad-run.txt`、`bad-ir.json` |
| 输入卫生探针 | `python output/t02-evidence/t02_probes.py` | **17 个探针，严格入口拦下 16 个**；唯一放行的是合法 v0.1 约束（被迁移而非拒绝）；宽松入口**无一**静默 `ok=true` | `output/t02-evidence/t02-probes.json` |

T00 基线对比（同一批用例）：基线 `{"ok": true, "codes": []}` →
现在全部有稳定错误码 + 字段路径。

**回归证明"没改电气内容"**：T01 与 T02 两次生成的 **kicadxml 网表逐引脚一致**
（10 网络：BST/COMP/COMP_RC/EN/FB/FREQ/GND/SW/VIN/VOUT，GND 13 个引脚含
U1.9，SW 4 个引脚含 C4.2），ERC 都是 0/0。

### 未完成 / 未验证

1. **历史探针无法原样重跑**（诚实记录，不是本任务可修的）：两处具体原因——
   (a) 它绑定 v0.2 之前的数据形状，读 `raw['constraints'][...]` 会 KeyError；
   (b) 它对 M1 的 v0.1 示例跑第一个用例就抛
   `ValueError: 器件 'MP1584EN' 没有名为/编号为 'PGND' 的引脚`——因为
   M1 的示例用了 T01 已删除的凭空引脚。为不修改只读的 `review/`，
   另写 `output/t02-evidence/t02_probes.py` 重实现 S7 族用例，
   并在临时副本中记录 (b) 的真实现象（`probe-v01-rerun.json`）。
2. **两处校验在进入 `validate` 之前就拒绝**：非有限数走 JSON 字面量路径时
   由 decode 报 `E-IR-DECODE-004`；若经 `from_dict` 传入 `float("nan")`，
   语义层同样报 `E-IR-DECODE-004`（已测）。**未测**：把 NaN 塞进
   `conditions` 的取值——`conditions` 目前不做数值校验，T03/T07 需要时补。
3. **质量/拓扑族（S9 的一部分）仍未覆盖**：器件评级、拓扑匹配
   （`regulator_topology_mismatch`、`async_buck_without_diode`）、
   引脚跨网络重复、footprint 覆盖、注入目录未生效——T00 复现的这些问题
   在 T03/T04 处理，本轮**未动**。
4. **`ports`（外部端口/驱动来源）已在 v0.2 定义并测试往返，但示例未用、
   生成器未消费**：PWR_FLAG 的来源证明是 T03 的工作。
5. **`gen.py` 的"全部通过"仍不可信**：它只表示 ERC 无错误；网表对账（S3）
   与 BuildReport 状态（T07）未接入。本轮只加了结构闸门。
6. `gen.py` 输出现改为显式 UTF-8（`reconfigure`）：此前在 Windows 控制台
   编码下重定向会写出别处读不出的字节，使"可复现输出"失真。T07 落报告层时
   需要一并统一。

### 下一步

T03：统一连接解析——必接引脚、显式 NC（只有 `nc` 里列的引脚才允许悬空）、
引脚归属唯一性（与网络书写顺序无关）、供电来源证明（外部 `ports` 或
串联无源元件路径），以及 T01 顺延的四个反例子图测试（错误 EN、BST–GND、
缺 FREQ、并联 COMP RC）。

---

## T03：统一连接解析（必接脚 / 显式 NC / 归属唯一 / 供电来源 / 必需子图）

**任务**：T03 / 连接语义单点化 + 生成器与校验器共用同一份判定
**日期**：2026-09-26

### 起点

- 上一任务 T02 完成（121 tests 全绿），其证据在 `output/t02-evidence/`；
  环境 KiCad 10.0.6。
- 待修问题（T00 复现，`review/probe-results.json`；T01 顺延四项）：
  - `duplicate_pin_across_nets`：同一只脚挂 VIN 与 GND，观察值
    `{"ok": true, "codes": [], "rendered_U1_pin6_net": "GND"}`——**后写的赢**；
  - `missing_required_vin`：漏接 VIN，观察值
    `{"ok": true, "codes": [], "generated_no_connect_count": 1}`——不但放过，
    生成器还**自己补了一个 no_connect**，把缺陷画成设计意图；
  - `injected_parts_disconnected`：注入目录后观察值
    `{"ok": true, "generated_no_connect_count": 9}`——符号用注入表、连线用
    全局表，两边各算一套；
  - T01 顺延的四个反例子图（EN 直连 12V、自举电容接 GND、缺 FREQ 电阻、
    COMP 并联 RC）在 M1 时**全部能通过**。

### 问题（在 T03 实现过程中自己暴露出来的）

**生成器的预检只覆盖"能不能连上"，不覆盖"连得对不对"。** 第一版
`connections_of()` 只跑连接冲突 + 供电来源，于是四个子图反例虽然都被
诊断出 error，`render()` 却照常产出一张图——"校验说不行、生成照做"，
正是 T00 认定的那类缺陷，只是换了位置。修法不是给每条规则打补丁，而是
把生成器的预检面**等于**检查包的全部内容（新增 `checks/preflight.py`），
并加 `tests/test_connectivity.py::test_subgraph_violations_also_block_generation`
钉死"生成器拒绝生成的那份判定 == 校验器报出来的那份"。

### 实现

**新增：`checks/` 包（与 EDA 后端无关，校验器与生成器共用）**

| 文件 | 行数 | 职责 |
|---|---|---|
| `checks/connectivity.py` | 237 | 引脚↔网络索引的**唯一**来源：`Claim`(ref/number/net/path) → 冲突检测 → nc 语义 → 必接脚 → 孤儿引脚 |
| `checks/supply.py` | 175 | 供电来源证明：power_out 引脚 / `drives=true` 外部端口 / 从已证明网络引来的**串联无源元件**路径（闭包） |
| `checks/subgraph.py` | 140 | 把器件上的 `connection_rules` 跑成诊断；**连接有冲突时不跑**（图不可信，先修冲突） |
| `checks/preflight.py` | 43 | 生成器/校验器的统一入口：`raise_on_error=True` 时不产出文件 |
| `parts/rules.py` | 49 | 规则的数据定义（纯数据，避免 partsdb→checks 的循环导入） |

**解析顺序**：先收集**全部** claim（不覆盖）→ 同网重复 `E-IR-NETS-009` →
跨网冲突 `E-IR-PARTS-010`（诊断里给出全部争用者，冲突脚**不进入索引**）→
nc 声明 `E-IR-PARTS-012`/`013` → 必接脚 `E-IR-PARTS-011` → 孤儿 `W-IR-NETS-002`。

**供电证明的判据**（`netclass="power"` **不是**证据）：
`power_out` 引脚 → 直接证明；外部端口 `drives=true` → 直接证明；
串联元件（**只认电感与电阻**：电容隔直、二极管与方向有关）→ 传递证明。
三条都不成立 = `E-IR-NETS-005` 硬错误，**并且不会补偿一个 PWR_FLAG**
（补标志只会让 ERC 闭嘴，不能证明它有电）。

**关键约定：只有 `components[].nc` 里显式声明的引脚才允许悬空。**
`MP1584EN` 的 EN 带 `nc_condition`（资料允许悬空），VIN/GND/SW 等不带，
声明它们 nc 直接 `E-IR-PARTS-012`。生成器遇到既没进网络、也没声明 nc 的
引脚会抛错而非补 `no_connect`。

**示例的 `ports`（v0.2 新增字段，T02 已定义、T03 首次消费）**：
`VIN_IN`(drives) 证明 VIN、`GND_REF`(drives) 证明 GND、`VOUT_OUT` 只是
输出声明——VOUT 的来源由 `U1.SW →(power_out) L1 → VOUT` 这条串联电感
路径传递证明，**不因为没有直接 power_out 就误报**。

**器件侧的规则带上资料出处**（`parts/partsdb.py`，MP1584EN 四条）：
BST–SW 必须跨接电容（第 4 页）、FREQ 必须经电阻对地（第 9 页）、
COMP 必须"电阻串联电容"到地而非并联 RC（第 13 页 Table 3）、
EN 不得与 VIN 同网（第 2 页绝对最大值 6V）。

**改动**：`ir/validate.py` 收敛到 `preflight()`（删掉自己那套调用）；
`backend/kicad/schematic.py` 的 `connections_of()` 改为 `preflight(raise_on_error=True)`，
`render()` 全程用 `conn.pin_net`，不再有第二处引脚解析。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **149 tests, OK, 0 fail, 0 skip**（T02: 121 → 149，新增 28） | 本页 |
| 层级审计 | `grep -rn "\.pins" backend/ ir/ checks/ …` | 构造 pin→net 的地方**只有** `checks/connectivity.py`（decode 只做语法、schema 只做序列化） | 本页 |
| 真跑生成 + ERC | `python gen.py examples/buck_12v_to_3v3.json -o output/t03-evidence/gen` | 退出码 **0**；KiCad 10.0.6 **0 错误 0 警告** | `output/t03-evidence/gen-run.txt` |
| ERC 原始报告 | `kicad-cli sch erc --format json` | `sheets[0].violations` 长度 **0** | `output/t03-evidence/erc-real.json` |
| 网表回归 | `kicad-cli sch export netlist --format kicadxml` | **与 T02 逐字节一致，仅 `<source>`/`<date>` 两行元数据不同**（10 网络 / 17 器件） | `output/t03-evidence/netlist-real.xml` |
| 反例闸门（8 个） | `python gen.py output/t03-evidence/negative/*.json` | 8 个全部 **退出码 1、不产出任何文件**，各报专属错误码 | `output/t03-evidence/negative/run.txt` |
| 正例放行 | 同上，`optional-float-ok.json` | **退出码 0**，ERC 0/0，图上**恰好 1 个** `no_connect`，且它是声明过的 `U1.2(EN)` | 同上 |
| 连接/供电探针 | `python output/t03-evidence/t03_probes.py` | 15 个探针：生成器**拒绝 12 个、放行 3 个**（放行的 3 个本就应该放行） | `output/t03-evidence/t03-probes.json` |
| T02 行为回归 | `python output/t02-evidence/t02_probes.py output/t03-evidence/t02-probes-under-t03.json` | 17 个探针中 **2 个结论变化**，均为 T03 新增检查在同一输入上多报**真实**缺陷 | `output/t03-evidence/t02-probes-delta.txt` |
| PWR_FLAG 对照实验 | `kicad-cli sch erc` 对 control / ablated 两份同目录原理图 | 有标志 **0 条**，无标志 **3 × `power_pin_not_driven`**——标志是承重的 | `output/t03-evidence/pwrflag-ablation/` |

**反例闸门逐条**（`output/t03-evidence/negative/run.txt`）：

| 用例 | 错误码 | 退出码 | 产物 |
|---|---|---|---|
| `dup-pin`（U1.GND 又挂到 VIN） | `E-IR-PARTS-010` | 1 | 无 |
| `missing-vin`（漏接 VIN） | `E-IR-PARTS-011` | 1 | 无 |
| `nc-and-connected`（EN 既接又 nc） | `E-IR-PARTS-013` | 1 | 无 |
| `unproven-vin`（VIN 无来源证明） | `E-IR-NETS-005` | 1 | 无 |
| `h2-en-tied-to-vin` | `E-IR-ELECT-004` | 1 | 无 |
| `h3-bootstrap-to-gnd` | `E-IR-NETS-008` | 1 | 无 |
| `missing-freq-resistor` | `E-IR-NETS-008` | 1 | 无 |
| `h4-comp-parallel-rc` | `E-IR-NETS-008` | 1 | 无 |
| `optional-float-ok`（EN 摘线 + 声明 nc） | — | **0** | 生成，ERC 0/0，1 个 no_connect |

**验收对照**（T03 验收口径逐条）：
漏 VIN ✅ 阻止生成；VIN/GND 双重归属 ✅ 阻止生成且与书写顺序无关
（`test_reordering_nets_does_not_change_the_verdict`）；同一脚同时 NC/连接
✅ 阻止生成；明确合法的可选脚悬空 ✅ 通过并只画 1 个 NC；串联电感后的供电
✅ 不误报（`supply.proof["VOUT"]` = "经 L1(IND-POW-10uH-3A) 由 SW 引来"，
且 SW 有 power_out 引脚、不在 PWR_FLAG 集合里）。

### 未完成 / 未验证

1. **子图规则只在图完整时跑**：连接冲突与子图违规同时存在时只报连接类
   （有意的降噪，`test_rules_skipped_when_graph_is_broken` 钉住）。代价是
   修完冲突要再跑一次才能看到子图问题。
2. **串联供电路径只认电感与电阻**：电容隔直、二极管与方向相关，都未做。
   若某设计确实靠二极管供电，会保守地报 `E-IR-NETS-005`——**未测**到实例，
   属已知保守误报，改法见 T04。
3. **`parts=` 注入仍是库级 API**：`gen.py` 没有暴露目录注入（T00 的
   `injected_parts_disconnected` 在库级已修：注入表同时决定符号与连接，
   探针里 `no_connect` 从 9 降到 0），命令行入口留给 T04。
4. **T04 的错误码仍未实现**：`E-IR-PARTS-014/015/018` 等（评级核对、拓扑
   匹配、footprint 覆盖）与 `regulator_topology_mismatch`、
   `async_buck_without_diode` 两条 T00 探针仍在红。
5. **`conditions` 里的非有限数仍未校验**（T02 遗留，未动）。
6. **网表对账（S3）与 BuildReport 未接入**：`gen.py` 的"全部通过"目前仍只
   表示"ERC 无错误"，不含网表与 IR 的逐引脚对账（T05）、不含报告层（T07）。
7. **PWR_FLAG 的逐个网络必要性未验证**：写了对照实验（`output/t03-evidence/pwrflag-ablation/`，
   同目录同工程文件，唯一变量 = 标志岛）：去掉 3 个标志后 ERC 从 0 条变成
   **3 × `power_pin_not_driven`**（另 3 条 `pin_not_connected` 是"删导线留下
   另一端电源符号"的副产物，不是标志的功劳）。结论只到"标志是承重的"这一步：
   **未做**逐个网络的消融，因此"恰好这 3 个网需要标志、一个不多一个不少"
   仍是推理。同理，第一版实验因漏拷 `.kicad_pro` 让 control 自己报了 17 条
   `lib_symbol_issues`——已记在实验 README 里，提醒别把它当成缺陷。

### 下一步

T04：目录注入贯通到所有辅助函数（`render`/`_instance`/`_net_of_map` 一族）、
有效 footprint 与允许集校验、额定值/参数一致性核对、引入 `ResolvedIR`，
并实现 T00 仍红的器件/拓扑族（`component_ratings_ignored`、
`regulator_topology_mismatch`、`async_buck_without_diode`、
`ignored_footprint_override`）。
## T04：器件事实核对（有效封装 / value 一致性 / 额定值 vs 需求 / 拓扑支持）

**任务**：T04 / 目录注入贯通到落盘 + 器件事实核对 + `ResolvedIR` 单点解析
**日期**：2026-09-26

### 起点

- 上一任务 T03 完成（149 tests 全绿），证据在 `output/t03-evidence/`；
  环境 KiCad 10.0.6。
- T00 复现、T03 结束时仍未修的四条（`review/probe-results.json`，只读未改）：

  | 探针 | 历史观察值 | 期望 |
  |---|---|---|
  | `ignored_footprint_override` | `{"ok": true, "codes": [], "override_present": false}` | 要么按请求的封装产出、要么拒绝 |
  | `component_ratings_ignored` | `{"ok": true, "codes": []}` | vin 90–110V / 99A 用在 4.5–28V / 3A 的芯片上必须拒 |
  | `regulator_topology_mismatch` | `{"ok": true, "codes": []}` | 异步芯片当同步 buck 用必须拒 |
  | `async_buck_without_diode` | `{"ok": true, "codes": []}` | 异步 buck 缺续流二极管必须拒 |

- T03 遗留：`parts=` 注入只是库级 API，且 `write_schematic`/`_symbol_lib_text`
  在落盘那一步把注入目录换回全局表。

### 问题（在 T04 实现过程中自己暴露出来的）

1. **预检面与检查集不同步（第二次犯同一类错）**。器件事实核对第一版写在
   `ir/resolved.py::resolve()` 里，而校验器走的是 `checks/preflight.py`——
   于是校验器完全看不到器件事实诊断：删掉电感的 `test_buck_without_inductor`
   立刻变红，`E-IR-PARTS-007` 从 validate 结果里消失。这与 T03 修过的
   「两处各判一遍」是同一个病，只是方向相反。修法：器件事实进
   `preflight()`（返回 `(连接, 供电, 器件事实, 诊断)`），`resolve()` 只消费
   它的返回值，`PartFacts` 一并返回，不再有第二个入口。
2. **同一问题两个来源**。`ir/validate.py` 里还留着 M1 时代的弱化副本
   「buck 必须有电感」（只看 role，不看具体拓扑，异步 buck 的续流二极管
   完全不查）。删除，`E-IR-PARTS-007` 的唯一实现是
   `checks/parts.py::_topology`——它按拓扑要求查（`async_buck` 要电感 +
   续流二极管，`sync_buck` 只要电感，`ldo` 不要）。
3. **测试 fixture 本身不成立**。`test_generation_uses_injected_catalog_end_to_end`
   的 IR 写着 `async_buck`，器件表里却只有一个稳压器 + 一只电容——新检查
   正确地拒掉了它。这是 fixture 的错：该 IR 描述的就是「稳压器 + 输入电容」，
   即 LDO 电路。按它**实际是什么**改声明为 `power.ldo` / `ldo`
   （独立依据：异步 buck 的定义要求电感与续流二极管，这份 IR 里没有）。
   没有放宽检查、没有删断言、没有降低目标。
4. **`comp.category` 写在 IR `Component` 上**（应为 `part.category`），
   14 个测试报 `AttributeError`。已修。

### 实现

**新增 `checks/parts.py`（器件事实，与 `checks/` 其它模块同一约定：结论同时
喂校验器与生成器）**

| 检查 | 错误码 | 判据 |
|---|---|---|
| 有效封装 | `E-IR-PARTS-014` | IR 覆盖必须命中该料的**允许集合**；不命中就报错并拒绝，绝不用目录默认值顶替 |
| value vs 所选料 | `E-IR-PARTS-015` / `W-IR-PARTS-002` | `parse_magnitude` + `magnitudes_match`；>5% 不同 = 错，≤5% = 提醒 |
| 注释额定值 vs 目录 | `E-IR-PARTS-015` | `rating` 注释只许与目录**一致**，矛盾即错；**判定用量时用的始终是目录值** |
| 拓扑支持 | `E-IR-ELECT-005` | `part.supports_topology()` 为 `False` 才报；`None`（目录未声明）= 未知，记 unverified |
| 拓扑必需器件 | `E-IR-PARTS-007` | `TOPOLOGY_REQUIRED`：异步 buck 要电感 + 续流二极管 |
| 需求 vs 额定值 | `E-IR-ELECT-004` / `W-IR-ELECT-002` | 稳压器 vin/vout/iout 对推荐工作范围与**绝对最大值**分开判；电感额定电流；二极管电流 + 反向耐压；电容耐压对**两端实际电压** |

**新增 `ir/resolved.py`**：`resolve(ir, parts) -> ResolvedIR`
（`ResolvedComponent`：有效封装 + 来源 override/catalog/none、角色、value、
注释；`ResolvedIR`：连接、供电、诊断、`catalog.revision`、`unverified`）。
`render(ir, parts)` = `render_resolved(require_ok(resolve(ir, parts)))`——
生成器不再自己查目录。**未核对项显式列出**（缺工况 / 缺目录字段 / 电压
不可知），而不是退化成"通过"。

**改动**：`checks/preflight.py` 纳入器件事实并返回 4 元组；
`backend/kicad/schematic.py` 的 `_Placed` 带**有效封装**并一路传到符号实例
（`write_schematic` 也把 `parts` 传到底）；`ir/validate.py` 删掉 007 副本、
改 4 元组解包；`tests/test_parts_facts.py` 新增 22 个用例（合成审计目录
`AUDIT-*`，显式声明非真实器件）。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **171 tests, OK, 0 fail, 0 skip**（T03: 149 → 171） | `output/t04-evidence/tests.txt` |
| 真跑生成 + ERC | `python gen.py examples/buck_12v_to_3v3.json -o output/t04-evidence/gen` | 退出码 **0**；KiCad 10.0.6 **0 错误 0 警告** | `output/t04-evidence/gen-run.txt` |
| ERC 原始报告 | `kicad-cli sch erc --format json` | `sheets[0].violations` 为空 | `output/t04-evidence/erc-real.json` |
| T04 探针（4 反例 + 3 正例） | `python output/t04-evidence/t04_probes.py` | 生成器**拒绝 5 个、放行 2 个**（放行的两个本就应该放行） | `output/t04-evidence/t04-probes.json` |
| 覆盖正例真跑 | 同上（注入目录 + kicad-cli 导网表） | 实例 `instance_has_override: true`、网表 `(footprint "Audit:AltFootprint")`；同网表里 D1 仍是目录默认封装 | `output/t04-evidence/override/` |
| 电气内容未变 | `python output/t04-evidence/netlist_compare.py` | 引脚↔网络 **41/41 全同**、封装 **17/17 全同**、且全部等于目录默认值 | `output/t04-evidence/netlist-compare.txt` |
| T03 行为回归 | `python output/t03-evidence/t03_probes.py output/t04-evidence/t03-probes-under-t04.json` | 15 条探针**结论 0 条变化**；12 条仅拒绝消息措辞变化（预检范围扩到「…/器件事实」） | `output/t04-evidence/t03-probes-delta.txt` |

**T00 四探针 red → green**（本次观察值）：

| 探针 | T00 | T04 |
|---|---|---|
| `ignored_footprint_override` | `{ok: true, codes: [], override_present: false}` | `errors=[E-IR-PARTS-014]`，`render.generated=false` |
| `component_ratings_ignored` | `{ok: true, codes: []}` | `errors=[E-IR-ELECT-004]`，`render.generated=false` |
| `regulator_topology_mismatch` | `{ok: true, codes: []}` | `errors=[E-IR-ELECT-005]`，`render.generated=false` |
| `async_buck_without_diode` | `{ok: true, codes: []}` | `errors=[E-IR-PARTS-007]`，`render.generated=false` |

**验收对照**（T04 验收口径逐条）：
目录注入贯通 ✅（`render`/`write_schematic`/`_symbol_lib_text` 全走
`ResolvedIR`，正例见注入目录的端到端用例与探针）；有效 footprint 计算 ✅；
不允许的覆盖**明确拒绝**（不是忽略）✅；允许的覆盖在**实例与导出网表**里
都可见 ✅；value/rating 与所选料矛盾被检出 ✅；注释式 rating **不能**覆盖
目录额定值 ✅；原有 render 确定性测试保留，并新增"注入目录下仍确定性"用例 ✅。

### 未完成 / 未验证

1. **`unverified` 只被记录，还没进 BuildReport**（T07）。示例当前有两条：
   `C3`（COMP_RC 节点）与 `C4`（BST 节点）——这两处的直流电压静态不可知，
   **没有**用 `vin.max` 之类的估值去凑一个"通过"，记未核对。
2. **电容耐压的 `_exposure` 只看两端直流电压**：开关节点/控制节点一律
   判为不可知；纹波、开关尖峰、瞬态未计入。
3. **二极管反向电压只按 `vin.max` 判**（异步 buck 续流管关断时承受输入
   电压的近似），同步 buck 的体二极管与尖峰未考虑。
4. **电感的饱和电流/直流电阻、电容的纹波电流与 ESR 未纳入**——目录里
   也没有这些字段，属已知缺口（不猜）。
5. **电阻的功率/耐压未与需求对照**（示例里电阻没有工况电流可算）。
6. **`gen.py` 仍未暴露目录注入与 `unverified`**：注入目前是库级 API，
   命令行/报告层留到 T07 的 `pipeline/`。
7. **封装覆盖只校验"在允许集合内"**：没有校验覆盖封装的实际焊盘编号与
   引脚表是否一致（要靠 DRC / 后续 footprint 级校验）。
8. **ERC 报告里的 `ignored_checks` 未被呈现**（本次 4 条，如
   `single_global_label`）——当前 `backend/kicad/cli.py::erc` 只累加
   `violations`，被 KiCad 主动忽略的检查没有出现在结论里。这是 T06 的范围。

### 下一步

T05：把 `backend/kicad/netlist.py` 的解析与**逐引脚双向对账**接进生产流程
（当前生产只有 ERC，没有"导出的网表是否等于 IR 的连接"这一步），并给
`export_netlist` 加 `--format kicadxml`。

## T05：导出网表的逐引脚双向对账（产物 == 意图）

**任务**：T05 / 把「导出的网表是否等于 IR 的连接」接进生产流程
**日期**：2026-09-26

### 起点

- 上一任务 T04 完成（171 tests 全绿），证据在 `output/t04-evidence/`；
  环境 KiCad 10.0.6。
- T04 遗留：`gen.py` 的「全部通过」只表示 **ERC 0 错误**。ERC 看的是原理图
  自己——它对 IR 一无所知；生成器把网画错（漏线、并网、接错邻脚）时 ERC
  完全可能一条不报。缺的是「产物 vs 意图」这一层检查。

### 问题（在 T05 实现过程中自己暴露出来的）

1. **变异测试会假通过（最隐蔽的一个）**。第一版反例用字符串替换做变异：
   `replace('<node ref="R4" pin="1" pinfunction="1" pintype="passive"/>', "")`。
   真实 `kicadxml` 的 `<node>` **不带** `pinfunction` 属性（只有芯片的功能脚
   才有），一个字符都没匹配上——"删掉一只脚"其实什么都没删，测试照样绿，
   却什么都没验证。修法：变异走 XML 树，并且 `mutate()` **断言变异真的
   改变了文档**（`ET.tostring` 前后比较），改不动就直接失败。
2. **错误码没登记就构造**。`E-NET-006` 是 T05 新码，我直接用它构造
   `Diagnostic`，`diagnostics.py` 的登记表立刻抛 `KeyError`——这正是登记表
   存在的意义（不登记就构造不出来 = 拼错码变开发期错误）。已登记。
   `E-NET-005` 的定义同时从"未声明的电源符号/器件"放宽为"未声明的**对象
   （器件/引脚）**"：已知器件的多余引脚与多余器件是同一类事实的两个形态。
3. **重名网络把别的错误盖掉了**。第一版在归一化网络名有歧义时 `continue`
   跳过该网的全部节点——于是"同一只脚同时落在 /FB 和 GND"只剩一条
   「网络名重复」，接错脚这件事**没被报出来**。修法：重名由 `E-NET-002`
   报，但逐引脚检查**照跑**（网名与 IR 期望不一致就是不一致）；「IR 的每只脚
   都出现过」也改成按**引脚集合**判，否则重名网里的脚会被误报成"少画了"，
   把一种错误说成另一种。
4. **改默认格式打断了历史证据脚本**。`export_netlist` 默认格式改为
   `kicadxml` 后，`output/t04-evidence/t04_probes.py` 里用
   `(footprint "…")` 判定网表的探针立刻变红（它拿到的是 XML）。判据与基线
   一个字没改，只在该探针里显式写 `fmt="kicadsexpr"`；改后 T04 的 7 条观察值
   与 T04 记录**逐字段相同**。教训：改公共默认值要跑一遍历史证据脚本。
5. **手写 s-expr 解析器与参考实现的交叉验证**：`.net` 顶层是
   `(export … (nets (net …)))`，网络**不在**根的下级而是再嵌一层。第一版
   `children(root, "net")` 返回空表。发现它的正是"两条解析路径必须给出同一
   结果"这条判据。

### 实现

**新增 `backend/kicad/netlist.py`（生产模块，非测试专用）**

| 部分 | 内容 |
|---|---|
| 解析 | `parse_kicadxml(text)` 用**标准库 XML**（不用正则：转义/嵌套/属性顺序都不是正则能可靠处理的）；空文件、截断、缺 `<components>`/`<nets>`、`<net>` 为空都抛 `NetlistError`——**没有内容不等于零违规** |
| 计数 | `net_name_counts` / `node_counts` **先数后比**，不提前 `set()` 去重，否则"导出结果自相矛盾"会被伪装成"集合一致" |
| 归一化 | `normalize_net_name` 只剥**恰好一个**前导斜杠（根图纸局部标签 `/NAME` ↔ IR 的 `NAME`）。不用 `lstrip("/")`：那会把 `/a/b` 变 `a/b`、把 `//X` 与 `X` 并成一个，正好掩盖层级与歧义 |
| 对账 | `reconcile_resolved(rir, netlist, aux_refs=…)` 双向：IR→网表（网络/引脚是否存在、是否落在同一个网）、网表→IR（有无 IR 不知道的网络/器件/引脚） |
| 辅助符号 | `auxiliary_refs(schematic_text)` 从**本次生成的**原理图按 `lib_id` 取确定清单（`power:GND`、`power:PWR_FLAG`、`circuitos:PWR_<网络>`）；不按 `#` 前缀过滤——那会吞掉用户的器件 |

**错误码**：`E-NET-001` 不可解析 / `E-NET-002` 网络集合不一致（含归一化
碰撞）/ `E-NET-003` 节点数量不符（含重复记录）/ `E-NET-004` 引脚归属不同 /
`E-NET-005` 出现 IR 未声明的对象 / **新增 `E-NET-006`** 层级或多页网表
（当前策略是单页，见到就拒，不猜映射）。

**接入生产**：`gen.py` 导出 `kicadxml` → `read_netlist` → `reconcile`
（`aux_refs` 来自刚写出的 `.kicad_sch`）→ 有 error 就**退出码 4**，不再打印
「全部通过」。退出码 `0 成功 / 1 IR 语义 / 2 ERC 有错 / 3 结构不合格 /
4 对账失败`。`backend/kicad/cli.py::export_netlist` 增加 `fmt` 参数，默认
`kicadxml`。

**测试**：新增 `tests/test_netlist_reconcile.py`（29 例：归一化、解析失败
各形态、真实导出 0 假阳性、9 种变异各自稳定码、辅助符号按清单排除、
**生产入口退出码契约**）；`tests/test_schematic.py` 不再自带第二套网表
解析器——对账改走生产解析器，`parse_nets` 保留为 `.net` 格式的**独立参考
实现**，只用于和新写的 `parse_nets_sexpr`（生产 `sexpr.py`）交叉验证。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **201 tests, OK, 0 fail, 0 skip**（T04: 171 → 201） | `output/t05-evidence/tests.txt` |
| 真跑生成 + ERC + 对账 | `python gen.py examples/buck_12v_to_3v3.json -o output/t05-evidence/gen` | 退出码 **0**；ERC **0 错误 0 警告**；**对账 10 网络 / 17 器件，0 错误** | `output/t05-evidence/gen-run.txt`、`gen/` |
| 反例 + 正例控制 | `python output/t05-evidence/t05_probes.py` | 9 条反例**全部退出码 4** 且码与受影响对象符合预期；2 条正例控制（`identity` 退出码 0、`aux_exported` 按清单排除后 0 条） | `output/t05-evidence/t05-probes.json`、`t05-probes-summary.txt` |
| 两种格式语义一致 | `python output/t05-evidence/compare_formats.py` | kicadxml 与 kicadsexpr 两条解析路径：**41 个引脚↔网络完全一致**，网络集合无差 | `output/t05-evidence/format-compare.txt` |
| T05 未改变电气内容 | 逐字节比较 `.kicad_sch` | 与 T04 **完全相同**（sha256 `b6d421fa…`，70242 字节） | — |
| T03 行为回归 | `python output/t03-evidence/t03_probes.py output/t05-evidence/t03-probes-under-t05.json` | 15 条探针观察值**完全一致** | `output/t05-evidence/t03-probes-under-t05.json` |
| T04 行为回归 | `python output/t04-evidence/t04_probes.py <abs>/t04-probes-under-t05.json` | 7 条探针观察值**完全一致**（需上述一处格式适配） | `output/t05-evidence/t04-probes-under-t05.json` |

**验收对照**（T05 验收口径逐条）：
"返回成功但网表为空/与 IR 不符，必须非零退出" ✅（`empty_nets`、`truncated`、
8 种不一致均退出码 4，且来自 `gen.main()` 本身）；"真实正确样例成功" ✅
（`identity` 退出码 0、`gen-run.txt` 退出码 0）；"对账差异给出稳定 code 与
受影响的 ref/pin/net" ✅（见探针表：`FREQ/R4.1/U1.6`、`R2.1`、`X9`、`C1.7`…）；
"正式 build 必须调用此模块、不能只在某个测试中对账固定示例" ✅（`gen.py`
的生产路径调用，且 `gen.py` **不再打印「全部通过」除非对账通过**）。

### 未完成 / 未验证

1. **对账只覆盖单页原理图**：层级/多页直接拒（`E-NET-006`），没有实现层级
   映射——这是**有意的**，不猜。
2. **只比对连接，不比对参数值**：网表里的 value 读了、记录了（
   `ExportedComponent.value`），但没和 IR 的 value 做对照。器件值写错目前
   ERC 与对账都拦不住（T04 的 `E-IR-PARTS-015` 只管 IR↔目录）。
3. **对账不看封装**：`E-NET-*` 里没有"网表里的 footprint 与有效封装不一致"
   这一条。T04 的覆盖正例是靠探针单独验的，不是生产链路验的。
4. **`auxiliary_refs` 依赖原理图文本结构**（`lib_symbols` 与实例之分、四种
   `lib_id`）。换了生成策略或 KiCad 改变导出行为时要重核。
5. **ERC 报告里的 `ignored_checks` 仍未呈现**（本次 4 条）——T06 的范围。
6. **探针脚本只做了网表侧的变异**："渲染器画错"这一侧没有被注入验证
   （即在写出原理图的那一步制造错误），证明的是"一旦不一致就拦得住"。
7. **退出码 4 时工程文件仍已落盘**（与 ERC 有错时的行为一致）。是否要在
   对账失败时删除产物，留到 T07 的 `pipeline/BuildReport` 一起定。
8. **README/DESIGN 仍写着 T05 之前的流程**（README 的 M1 行「34 测试全绿」、
   「网表 7 网与 IR 逐引脚一致」是 M1 当时的验收口径）。按计划留到 T08
   与全量回归一起改，避免同一句话改两遍。

### 下一步

T06：ERC 适配器——逐违规诊断、报告完备性/新鲜度检查（`E-ERC-003`）、
按规则 id + 对象键的豁免白名单，并把 `ignored_checks` 呈出来（否则
"0 错误 0 警告"会掩盖"有些检查根本没跑"）。

---

## T06：ERC 适配器（逐违规诊断、报告对应性、登记制豁免）

**任务**：T06 / 让 ERC 的结论可核对、可追溯，且**不把「没跑」说成「没错」**
**日期**：2026-09-26

### 起点

- 上一任务 T05 完成（201 tests 全绿），证据在 `output/t05-evidence/`；
  KiCad 10.0.6。
- T05 遗留 #5：ERC 报告里的 `ignored_checks`（实测 4 条）没有任何出口——
  也就是说"0 错误 0 警告"这句话，工具自己知道它没覆盖全部检查，而我们的
  报告里看不出来。
- 更根本的：旧实现（`cli.erc()`）只会数 `len(errors)`。**一次违规可以带多个
  items**，按 item 展开会虚增数量、把 items 丢掉会少报；报告是**文件**，
  可能是上一次跑剩的；JSON 坏了会被当成"没有违规"。这些都能让 ERC "通过"。

### 问题（在 T06 实现过程中自己暴露出来的）

1. **探针自己假通过（又栽在同一个坑上）**。构造"空 items / 未知严重级别 /
   违规缺字段"三种坏报告时，我把一条**违规**直接当**整份报告**写进了文件，
   于是三份都被"缺顶层字段"拦下——**通过的理由是错的**，实际一个预期缺陷
   都没测到。修法两条：变异必须包进完整报告骨架；判据改成"拒绝理由里必须
   出现预期缺陷的字样"，而不是"抛了异常就算过"。T05 的教训（变异要自证）
   换了张皮又出现一次。
2. **`gen.py` 在核验失败时抛栈而不是退出（真缺陷）**。探针 [4] 用一个"永远
   失败"的 kicad-cli 替身跑生产入口，得到未捕获的 `KicadError` traceback：
   ERC 已经失败的情况下，`gen.py` 仍然去调
   `export_netlist(..., check=True)`。异常类型是对的、退出码却丢了——
   而退出码才是调用方（脚本/CI）唯一能看的东西。修法：ERC 有 error 就**先
   返回 2**（前提已不成立，不必再导出网表），并把 `export_netlist` 的
   `KicadError` 接成诊断 + 退出码 2。回归测试见
   `tests/test_netlist_reconcile.py::TestEntrypointToolFailure`。
3. **只拷 `.kicad_sch` 会得到 17 条假告警**。第一版"删一条导线看 ERC 报什么"
   的扫描把原理图单独拷出来跑，结果 44 个用例**每一个**都带 17 条
   `lib_symbol_issues`（找不到符号 'circuitos'）——那是缺 `circuitos.kicad_sym`
   与 `sym-lib-table` 的假象，把真正的信号（每次删线都产生 error）淹了。
   整目录拷贝后同一份图的基线是 **0 违规**。所以：`verify` 的输入必须是一个
   自包含的工程，不是一张孤零零的原理图。
4. **`verify` 只"跑"不足以说明报告可信**。报告是文件：陈旧（上一次跑的）、
   张冠李戴（source 是别的图）、版本不同（别的 KiCad 产的）、
   `included_severities` 里没有 error——四种都能让一份"0 错误"变得毫无意义。
   全部归到 `E-ERC-003`，并在跑之前**先删旧报告**（否则工具没跑成时读到的是
   上一次的结果）。
5. **报告坏了 ≠ 工具没跑起来**。第一版把两者都写成 `E-ERC-002`，处置不同却
   共用一个码：一个是"KiCad 起不来/超时/非零退出"，另一个是"报告字节不可信"。
   拆成 `E-ERC-002`（工具）与 `E-ERC-001`（报告）。
6. **错误码登记表又拦了一次**：`E-ERC-005`、`W-ERC-003` 是新码，不登记就
   `KeyError`——这是 `diagnostics.py` 的设计意图（登记表是"码不许漂移"的闸）。
7. **绕开英文措辞做判据**。`tests/test_schematic.py` 原本用
   `"Input pin not driven" in w` 之类的英文子串放行"已知告警"：换个 KiCad
   版本消息一改就失效，而且把"没登记过的告警"和"已登记的"一起放过了。
   改成按**规则 id + 对象键**判，未登记一律 `W-ERC-002`。
8. **一个"不该被误拒"的细节**：真实报告的 `date` 是工具写的，而新鲜度检查
   拿它跟原理图 mtime 比。容差 2 秒（同一台机器连着写出来，秒级抖动能过），
   并把 `ignored_checks` 里**清单之外**的禁用项设计成"只记录不告警"——
   否则未来版本新增一个检查项就会让所有工程变红。

### 实现

**新增 `backend/kicad/erc.py`（ERC 的唯一入口；`cli.erc()` 已删）**

| 部分 | 内容 |
|---|---|
| 报告模型 | `ErcItem` / `ErcViolation` / `ErcReport`；`violation.object_id = "<规则>#<对象 uuid 列表>"`，空 items 时是 `-` |
| 解析 | `parse_report` 对**空/非 JSON/顶层非对象/缺顶层字段/sheets 空或非列表/sheet 缺 violations/违规缺字段/未知严重级别/items 非列表**全部抛 `ErcReportError`——"没有内容"不等于"零违规" |
| 逐违规 | `diagnostics`：error → `E-ERC-004`（**永不可豁免**）；warning 命中登记 → `W-ERC-001`（带理由+审查记录）；否则 `W-ERC-002`。**一次违规一条诊断**，items 只进消息不拆计数 |
| 对应性 | `check_report`：source 名不符 / `included_severities` 缺 error / kicad_version 与当前 CLI 不符 / 报告文件或 `date` 早于原理图（容差 2s）→ `E-ERC-003` |
| 未跑检查 | `ignored_checks` 里出现 `IMPORTANT_CHECKS`（`lib_symbol_issues`、`pin_not_connected`、`power_pin_not_driven`，**来自实测违规类型**，不是抄文档）→ `W-ERC-003`；清单外的只记录 |
| 工具边界 | `run`：先删旧报告；找不到 CLI / 超时 / 非零退出 / 退出 0 但没生成报告 → `E-ERC-002`；报告结构不符 → `E-ERC-001` |
| 豁免 | `erc_allowlist.json` + `load_exemptions`：条目必须有 `type`/`object_id`/`reason`/`reviewed`，级别必须是 warning；缺字段或想豁免 error → `E-ERC-005` 且**该条目不生效** |

**错误码**：`E-ERC-001` 报告不可用 / `E-ERC-002` 工具不可用 / `E-ERC-003`
报告与输入不对应 / `E-ERC-004` 未豁免的错误 / **新增 `E-ERC-005`** 豁免条目
非法 / `W-ERC-001` 已登记豁免的告警 / `W-ERC-002` 未登记的告警 /
**新增 `W-ERC-003`** 重要检查本次未运行。

**接入生产**：`gen.py` 用 `erc_mod.verify()` 取代 `cli.erc()`；打印违规数、
诊断，并**把未运行的检查印出来**；`erc_errors` 非空 → 退出码 2（且不再导出
网表）。`cli.erc()` 删除，`cli.py` 只留 `erc_report_path()`。

**测试**：新增 `tests/test_erc.py`（**56 例**：解析各形态的拒绝、空 items 的
error 仍要挡、未知级别不猜、豁免清单非法条目、对应性四种、工具边界三种、
真实报告现跑）；新增 `tests/fixtures/erc/`——**kicad-cli 真实产出**的 3 份
报告 + `manifest.json`（sha256 与出处）+ `README.md`，并由
`TestRealReportFixtures`（12 例，**不需要装 KiCad**）核对指纹、出处与行为；
`tests/test_schematic.py` 的 ERC 判据改为按规则 id；新增
`tests/test_netlist_reconcile.py::TestEntrypointToolFailure`（2 例，锁住
问题 2 的修复）。

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **260 tests, OK, 0 fail, 0 skip**（T05: 201 → 260） | `output/t06-evidence/tests.txt` |
| 验收探针 | `python output/t06-evidence/t06_probes.py` | **5 节全通过**（真实报告 2 节 / 坏报告 8 形态 + 2 反向 / 入口 3 例 / 未跑检查 3 例） | `output/t06-evidence/t06-probes-run.txt` |
| 真实工程 ERC | 探针内现跑（`.kicad_sch` + `.kicad_pro` + 符号库整套） | **0 违规 0 诊断**，`ignored_checks` 4 条被印出 | `output/t06-evidence/gen-real.erc.json` |
| 真实**告警**报告 | 同上，`erc.rule_severities.pin_not_connected="warning"` + 删一条导线 | 报告 **1 error + 2 warning**（真工具产出） | `tests/fixtures/erc/real-warning-and-error.json` |
| 真实**未跑**报告 | 同上，`pin_not_connected="ignore"` | `ignored_checks` 多出 `pin_not_connected`，违规 0 → `W-ERC-003` | `tests/fixtures/erc/real-ignored-important.json` |
| 坏报告形态 | 探针 [3]（每份都落在 `_scratch/`） | 8 种形态**全部拒绝且理由对得上**；另有 2 条"不该拒"的反向用例 | `output/t06-evidence/_scratch/` |
| 入口退出码 | 探针 [4]：真 CLI / 永远失败的替身 / 退出 0 但写半截报告的替身 | **0 / 2 / 2**（后两者分别 `E-ERC-002`、`E-ERC-001`） | `output/t06-evidence/t06-probes-run.txt` |

**验收对照**（实施指南 T06 原文："mock 的空 items error、未知严重级别、缺字段
均不能假通过；同时用真实有效报告 fixture 验证不会把合法零违规报告误拒绝"）：

- "空 items error 不能假通过" ✅ —— 结构合法时**接受但照样报 `E-ERC-004`**
  （探针 [3] 末条：`诊断 ['E-ERC-004']`，空表才说明这条违规整条消失了）；
  items 不是列表则直接拒绝。
- "未知严重级别不能假通过" ✅ —— `fatal` 被拒，理由是"严重级别 'fatal' 未知"
  （不是碰巧因为别的字段不对而挂掉）。
- "缺字段不能假通过" ✅ —— 违规缺 `type` / 报告缺顶层 `sheets` / sheet 缺
  `violations` 三种都拒，且理由各自对得上。
- "真实有效报告不被误拒" ✅ —— 真实 0 违规报告 `diagnostics()` 为空、
  `check_report()` 也为空；且这个"空"是有意义的：同一段代码对"被禁用了
  `pin_not_connected`"那份**确实**报出了 `W-ERC-003`。

**两个实测出来的关键事实**（`tests/fixtures/erc/README.md` 有记录）：

1. `kicad-cli` 认 `.kicad_pro` 里的 `erc.rule_severities`：改成 warning，
   报告里的 severity 就是 warning；改成 ignore，该项进 `ignored_checks`。
   两条假设都是**跑了才知道**的。
2. **被禁用的检查会让"没违规"变成空话**：disable + 删导线那个用例里导线真的
   断了，`pin_not_connected` **一声不吭**，只剩 `power_pin_not_driven` 还在报。
   这正是 `W-ERC-003` 存在的理由。

### 未完成 / 未验证

1. **`erc_allowlist.json` 是空的**（`{"exemptions": []}`）——豁免通路只在探针
   与测试里验过，**生产上一条豁免都没有**。当前真实工程确实 0 告警，不需要
   豁免；等出现第一条时必须走"规则 + 对象 uuid + 理由 + 审查记录"。
2. **豁免只支持"具体 uuid"或 `"*"`**，没有"规则级 + 条件"（比如"只豁免某个
   网络上的"）。够用即止，不提前造。
3. **版本核对依赖 `kicad-cli --version` 输出与报告里的 `kicad_version` 字面
   相同**（实测都是 `10.0.6`）。若某版本 CLI 打印 `10.0.6-rc1` 而报告写
   `10.0.6`，会误报 `E-ERC-003`。已记为风险，未处理。
4. **新鲜度用本地时间戳比 UTC 字符串**：`date` 是 KiCad 写的本地时间
   （实测与文件 mtime 一致），`_parse_date` 按本地时区解析。跨时区/容器里
   改 TZ 可能误判——未验证。
5. **一次跑测不出"报告被换过"**：靠的是 source/版本/时间三对齐，没有内容
   哈希。同一秒内换掉报告仍可能通过。
6. **`W-ERC-003` 只在 `check_report` 里产生**，而它需要原理图路径存在；
   `verify` 之外单独调 `diagnostics` 不会得到它（设计如此，但调用方要知道）。
7. **`IMPORTANT_CHECKS` 三个 id 来自本工程实测**，不是 KiCad 的完整检查清单。
   图里出现新形态（层级、总线、仿真模型）时这份清单要重核。
8. **探针 [4] 的替身可执行文件是测试脚手架**，不是产品代码；它证明的是
   "工具失败 ⇒ 退出码 2"这条链路，不代表真 kicad-cli 会那样失败。
9. **退出码与产物策略仍未统一**（T07）：ERC 失败时工程文件已落盘、网表不再
   导出，这个组合是 T06 的临时取舍。
10. **README/DESIGN 仍写着 T05 之前的流程**，留到 T08 与全量回归一起改。

### 下一步

T07：`pipeline/`（`models.py` / `build.py` / `workspace.py`）——把 `gen.py`
里的流程收成 `BuildReport`：profile（generate vs schematic）、按 build 分目录、
退出码统一，并把 `ResolvedIR.unverified`、`catalog.revision`、ERC 的
`ignored_checks` 一并呈出来（否则这些"未验证"的事实会散落在 stdout 里）。

---

## 交接（本轮停止点）

**用户指示：skill 安装完成后停止。T00–T06 完成，T07/T08 未开始。**

- 交接报告：**`docs/HANDOFF.md`**（现状、交付物清单、已证实事实、开放项汇总、
  恢复步骤、硬约束、环境坑、风险、T07 接口调研结论）。
- 交接时复跑（本次）：`260 tests OK`（0 fail 0 skip）｜T06 探针 26/26 通过
  （追加记录 `output/t06-evidence/t06-probes-rerun.txt`，原 `t06-probes-run.txt` 未改）｜
  真实工程 ERC 0 违规。
- **代码仍未提交**：HEAD 仍为 `4942bf8`（M1），T00–T06 的 9 个改动文件
  （+1259/−380）与 24 个新模块全部在工作区里，未 commit 未 push（用户要求不自动提交）。
- T07 只做了接口调研，**没有落任何代码**；调研结论见 `docs/HANDOFF.md` §4.3。

---

## 修复轮：复核报告的 P1#1 / P1#2（交接之后，T07 之前）

**任务**：按独立复核报告先修两个 P1——目录自校验没进生产路径、catalog revision
漏掉影响电气结论的数据
**日期**：2026-09-26
**依据**：`docs/CODEX_REVIEW_T00_T06.md`（只读）
**范围**：只修这两个 P1，不碰 T07/T08（用户明确要求先修完再进 T07）

### 起点

交接之后，Codex 复核 T00–T06 给出两个 P1：

1. **P1#1 目录自校验没有进入生产路径**。`CatalogSnapshot.validate()` 早就实现了
   "符号脚号 ↔ 封装电气焊盘"的核对，也能真报出 `E-CAT-002`；但
   `ir.validate.validate()` 与生成器都只走 `checks.preflight`，**没有一处生产
   路径调它**。测量脚本（`output/p1-review-fix/before-fix.txt`，修复前跑）：
   `validate(ir, 损坏目录) → ok=True codes=[]`、`resolve` 同样 ok=True，
   `write_schematic` 照常产出图纸，里面真的写着 `(pin "10" ...)`。
2. **P1#2 catalog revision 漏字段**。`_part_electrical_dict()` 不含
   `connection_rules`，`_source_dict()` 不含 `Source.note`：删掉 MP1584EN 的
   **全部**连接规则、或改掉一条来源的 note，`revision` 都是 `c996b1b5dea49154`
   （一个字节没变）。

**为什么必须先修**：T07 要把 `catalog.revision` 写进 BuildReport 当审计证据。
带着这两个缺陷进 T07，等于把"一个漏字段的版本号 + 一条没生效的校验"固化进产物——
报告里写着"基于某个 revision 生成"，而那个 revision 表达不了电气要求的改动。

### 问题（本轮实现过程中自己暴露出来的）

1. **策略不能由异常类型决定**（报告原文即此）。旧 `_check_pads` 把读取器的
   `except Exception` 一律降级成 `W-CAT-001` 并返回 `ok=True`，于是"核对不了"
   与"核对通过"在返回值上无法区分；测试想在 `validate()` 外面捕 `FootprintError`
   来自行 skip 也**永远捕不到**（`tests/test_device_facts.py` 那条用例就是这么
   写的，随环境时对时错）。修法：新增 `CatalogValidationPolicy`
   （`pad_provider` + `require_pads`），**由调用方显式声明**这次要核对到什么程度。
2. **`parts/` 不许 import `backend/`**，所以"这台机器有没有 KiCad 封装库"这一
   判断只能在 `gen.py` 里做（`catalog_policy()`），再作为策略传下去。这条分层
   约束正是"策略必须由调用方给出"的根因，不是巧合。
3. **口径判断不能混用 `find_cli()`**。第一版想用"有没有 kicad-cli"来判，会踩
   T06 的验收探针：它用 **stub kicad-cli** 跑生产入口，断言"工具失败 → 退出码 2"。
   stub 目录里没有封装库，若策略仍按 `require_pads=True` 判，就会新增一条
   `E-CAT-006` 错误，**改变退出码**、探针碎在这里。改成按 `footprints_root()`
   判（能不能核对封装）——"能不能核对封装"与"要不要跑 ERC"是两个问题，答案
   可以不同。
4. **"未核对"需要第三个出口**。`Outcome` 原本只有 `diagnostics`，没有地方放
   "没核对"——这正是"未核对被读成通过"的结构原因。加了 `Outcome.unverified`
   （第三态），并把 `ResolvedIR.unverified`（器件事实层）与新的
   `catalog_unverified`（目录层）分开：后者若并进前者，会改掉
   `tests/test_parts_facts.py` 里那条精确列表断言（C3/C4）的语义。
5. **目录诊断进入生产路径后，4 条既有用例的断言变了**（260 → 280 例的过程中
   暴露）。逐条按"口径分层"处理，**没有放宽**：
   - `test_device_facts` 那条按报告要求重写（先判环境再 skip；用新策略 API），
     并加断言"核得了就必须真核对"（`unverified` 必须为空）；
   - `test_ir` / `test_decode` / `test_parts_facts` 的**精确集合断言**改成
     按**码**剔除 `W-CAT-001` 后比对，且 `test_ir` 那条额外断言"目录层警告数
     恰好等于带封装的目录记录数"——新警告仍然会让断言失败。
6. **`_rule_dict` 不接受非数据类**。第一版写的是 `not isinstance(rule, object)`
   （恒假，等于没查）。改成"是类型对象或不是数据类实例就 `TypeError`"：把一个
   来路不明的对象 `str()` 一下哈希进去，等于把改动吞掉——正是 P1#2 要防的静默。
   规则字段用 `dataclasses.fields()` 全量展开，新增字段自动进哈希（不会又漏一个）。
7. **revision 的可读标签**。短哈希不是审计证据的全部：`CatalogSnapshot.id`
   （`builtin@<rev>`）与 `name` 一起才是"哪份目录、哪一版"。报告要求 BuildReport
   两者都记（T07 落）。

### 实现

| 文件 | 改动 |
|---|---|
| `parts/catalog.py` | 新增 `CatalogValidationPolicy(pad_provider, require_pads)`；`validate(policy)` 返回 `Outcome(diags, unverified=…)`；核对不了时按策略走 `E-CAT-006`（阻断）或 `W-CAT-001` + 未核对项；`_part_electrical_dict()` 加入 `spec` 与 `connection_rules`（按 canonical 文本排序，顺序无语义）；`_source_dict()` 加入 `note`；新增 `_rule_dict`（数据类全字段展开）与 `_canonical_key` |
| `ir/resolved.py` | `resolve(ir, parts, *, catalog_policy)` **唯一**调用 `catalog.validate()`；目录诊断并入 `ResolvedIR.diagnostics`，未核对项进新字段 `catalog_unverified` |
| `ir/validate.py` | 不再自己查目录：归一化成 `CatalogSnapshot` 后调 `resolve()` 取结论；签名加 `catalog_policy`；`unverified = 器件事实 + 目录层` |
| `backend/kicad/schematic.py` | `render` / `write_schematic` 接受并转发 `catalog_policy`——目录核对不过时**一个文件都不写** |
| `gen.py` | 新增 `catalog_policy()`（按 `footprints_root()` 判）；校验与生成传同一策略；打印未核对项；结尾从无范围的"全部通过"改成"**本次核验通过（ERC 0 错 + 网表与 IR 逐引脚一致）**"+ 有未核对项时的范围声明 |
| `diagnostics.py` | 新增码 `E-CAT-006`；`Outcome` 增加第三态 `unverified`（`extend` 一并传递） |
| `tests/test_catalog.py` | **新增 20 例**（生产入口闸门 / 策略三态 / revision 灵敏度与稳定性） |
| `tests/test_device_facts.py`、`test_ir.py`、`test_decode.py`、`test_parts_facts.py` | 见"问题 5"：按口径分层修正，逐条写明理由 |

**关键设计决定：目录自校验核对整个快照**，不是"本次用到的记录"。理由写在
`CatalogSnapshot.validate()` 的 docstring 里：目录是本次构建的**输入**，revision
也覆盖全部记录——一份带坏记录的目录，其证据链已经不完整，与这份 IR 恰好没用
到那条记录无关。

**与报告"修复要求"的逐条对照**：

| 报告要求 | 落点 |
|---|---|
| 策略显式化，不靠异常类型决定行为 | `CatalogValidationPolicy`（`pad_provider`/`require_pads`） |
| 结构校验在生产 resolve 阶段跑，错误并入 `ResolvedIR.diagnostics` | `resolve()` 唯一调用点；`ir.validate`/`gen` 都从它取 |
| schematic 口径：有封装的记录必须真核对；读失败是阻断，不是成功态里的 warning | `require_pads=True` → `E-CAT-006`（error） |
| generate 口径：允许缺库，但必须记"未核对"，且 `passed_profile` 不得暗示做过原理图核对 | `W-CAT-001` + `Outcome.unverified`；`gen.py` 打印未核对项并声明结论范围（`passed_profile` 本身是 T07 的字段） |
| 生产入口回归：EP=10 目录必须让 validate/resolve/build 报 `E-CAT-002`，且不得写出"已通过"的构建 | `tests/test_catalog.py::TestCatalogGateReachesProduction`（三处入口 + 断言临时目录里**没有**文件） |
| 修正无 KiCad 库时的测试（结果稳定、意图与 API 一致） | `test_device_facts` 那条重写：先判 `footprints_root()` 再 skip |
| revision 覆盖 `connection_rules` 与 `Source.note` | `_part_electrical_dict()` / `_source_dict()` |
| 嵌套规则的稳定序列化 + 显式顺序规则 | `_rule_dict` + 按 canonical 文本排序（顺序变化**不**改 revision） |
| 变异测试（规则 id/kind/pins/parameters、note、pin、rating、footprint 改 → revision 变；只改商业字段 → 只 supply_revision 变） | `TestCatalogRevision`（含"只改报价 → revision 不变"与"规则顺序对调 → 不变"两条反向用例） |
| BuildReport 记 revision **与可读目录名/版本** | 记录要求已在此列出；字段留给 T07（见"未完成"） |

### 验证（都是本次真跑）

| 项 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **280 tests, OK**（T06: 260 → 280，+20 全在 `tests/test_catalog.py`） | `output/p1-review-fix/after-fix-tests.txt` |
| 修复后验证脚本 | `python output/p1-review-fix/verify_p1_fixed.py` | **13 项断言全部通过**（生产闸门 4 / 策略三态 4 / revision 5） | `output/p1-review-fix/after-fix-verify.txt` |
| 修复前复现（对照） | `python output/p1-review-fix/reproduce_p1.py` | 损坏目录 `ok=True`、`(pin "10")` 真写进图纸；两个字段的改动不改 revision | `output/p1-review-fix/before-fix.txt` |
| 真实生成（真 KiCad 10.0.6） | `python gen.py examples/buck_12v_to_3v3.json -o output/p1-review-fix/after` | 退出码 **0**；schematic 口径下 **0 条 CAT 诊断**（20 条带封装记录**真读过**封装文件）；ERC 0 错 0 警告；对账 0 错 | `output/p1-review-fix/after-fix.txt` |
| T06 验收探针（回归） | `python output/t06-recheck/t06_probes.py`（**副本**目录，原证据未动） | **5 节全通过**，含"stub CLI 工具失败 → 退出码 2" | `output/t06-recheck/` |
| 修复前基线测试 | `python -m unittest discover -s tests -t .`（**测试先写、实现未动**时） | `261 tests, FAILED (errors=1)`——新模块整块 import 失败（`CatalogValidationPolicy` 还不存在）。这正是"新用例对旧代码失败"的直接证据 | `output/p1-review-fix/baseline-tests.txt` |
| T06 交接基线 | 同上（修复轮开始前，交接时） | **260 tests OK** | `output/t06-evidence/tests.txt` |

**测试先行**：`tests/test_catalog.py` 是在实现新 API **之前**写的，第一次跑
`ImportError: cannot import name 'CatalogValidationPolicy'`——它同时是对旧代码的
失败证明（生产入口用例只用 plain dict，不需要任何新接口）。

**revision 变更是预期内的破坏性事实**：加了两个字段后内置目录的
`revision` 从 `c996b1b5dea49154` 变成 `9a6bc694906b648b`。当前**没有任何消费者
存过旧值**（BuildReport 未实现、证据文件里只有测量输出），所以不需要迁移；
但 T07 起任何写进产物的 revision 都必须在这次修复之后取。

**未核对 ≠ 通过，现在有两个出口**：`ResolvedIR.unverified`（器件事实：示例 2 条
C3/C4 耐压）与 `ResolvedIR.catalog_unverified`（目录层：无读取器时 20 条）。
`gen.py` 在结尾声明结论范围；"未核对就写进 pending 列表"留给 T07。

### 未完成 / 未验证

1. **`passed_profile` 与 pending 列表本身没做**——报告要求 4 的后半句
   （"generate 口径下把 footprint unverified 放进 BuildReport 的 pending，
   `passed_profile` 不得暗示原理图级核对"）是 T07 的字段，现在只有 stdout
   的范围声明。数据已经齐了（`catalog_unverified` / `unverified` / `catalog.id`）。
2. **BuildReport 记 `catalog.id`（含名字+版本）** 未落——同上，T07。
3. **目录诊断现在会进生产诊断流**：无封装读取器时 20 条 `W-CAT-001` 会出现在
   `ResolvedIR.diagnostics` 里（T06 之前不出现）。这是有意的（宁可吵不要静默），
   但 T07 profile 化之前，`--quiet`/摘要这类输出会变长，需要在 T07 的
   BuildReport 里收成一条 pending 汇总。
4. **策略只有两档**（有/无读取器 + 要不要真核对）。没有"只核对本次用到的记录"
   这种粒度——若 T07 需要增量构建（只核对变化的记录），要重新设计而不是加开关。
5. **`E-CAT-006` 只在 `require_pads=True` 时出现**，也就是只有真实 schematic
   口径才可能触发。本机没有"有库但读不出某条封装"的样本，这条路径只在测试里
   用抛异常的替身覆盖过（`tests/test_catalog.py::TestCatalogValidationPolicy`），
   **真实坏封装文件没测**。
6. **`examples/buck_12v_to_3v3.json` 之外的 IR 未回归**：修复只跑了示例与测试
   夹具（`tests/fixtures/`），没有第二份真实 IR 验证 strict 口径。
7. **T07 之前不要动 `gen.py` 的退出码语义**：`catalog_policy()` 依赖
   "工具失败 → 2"这条 T06 结论，统一退出码时要一并回归 T06 探针。

### 下一步

**T07**（按用户指示，修完两个 P1 才进）：`pipeline/` 的 `models.py` / `build.py` /
`workspace.py`——profile（generate vs schematic）、按 build 分目录、`BuildReport`
落盘（含 `catalog.id` + `revision` + `unverified`/`catalog_unverified` → pending）、
退出码统一、`gen.py` 薄化。本轮修好的两个 P1 正是 T07 的**输入契约**：报告的
"已验证/未核对/失败"三态，数据源就是 `ResolvedIR` 的这两个 unverified 字段与
`CatalogSnapshot.id`。


## T07：构建流程、profile 与报告（Codex，2026-09-26）

起点：两个 P1 已修复并独立复核；280 tests 基线；HEAD 原有未提交修改全部保留。用户明确授权实现 T07、交接并更新 DeepSeek 指引。本轮没有 commit/push，也没有执行 T08 图纸视觉验收。

### 实现与决策

新增 pipeline/models.py、build.py、workspace.py；gen.py 薄化。严格解码后只 resolve 一次，validate_resolved/write_resolved/reconcile_resolved 共用同一快照。自查发现原排版 _plan 内部经 _island_offset 再次 resolve，已显式传入 flagged 消除；新增测试禁止后端偷偷再解析。

状态机八阶段 input/tool/catalog/resolve/generate/erc/netlist/publish；五种状态。generate 仅生成；schematic 强制真实封装读取/支持版本/ERC/网表，不按环境降级。auto保留交互用法，CI须显式schematic。实际证据只覆盖KiCad10.0.6，因此版本白名单先限定到10.0.6。未登记ERC告警与重要检查禁用会阻断；普通设计warning保留。

统一退出码0/1/2/3/4（profile成功/输入设计/产物验证/工具报告/I/O内部）。未知版本用unsupported，缺工具用error。旧真实入口测试改mock边界到pipeline.cli/erc，篡改网表仍由真ERC先验证，失败断言按新契约适配，未删除负例。空网表为报告错误3，有效网表不一致为2。

每次随机build ID；staging后目录改名发布。成功passed、失败failed诊断材料；不覆盖旧结果。报告/manifest记录源码指纹、输入原文/规范化hash、catalog.id/revision/supply_revision、工具版本、pending与unverified、ERC ignored_checks、网表统计、产物hash。manifest包含report哈希但不包含自身。输出无法写报告时stderr打印完整JSON。发布失败保留故障staging，不形成passed目录。

### 实跑证据

环境Python3.14.7、KiCad10.0.6。输出证据均在output/t07-evidence/。

| 命令/验证 | 结果 | 文件 |
|---|---|---|
| python -m unittest discover -s tests -t .（真实KiCad） | 301 tests OK，0 skip，74.681s | tests-kicad.txt |
| 同命令，COS_KICAD_CLI指向不存在文件 | 301 tests OK，35 skipped，6.801s | tests-no-kicad.txt |
| python -m unittest tests.test_pipeline tests.test_schematic -q（最后排版改动后，真实KiCad） | 40 tests OK，0 skip，15.340s | final-affected-tests.txt |
| python -m tests.t06_acceptance | 27断言通过，退出0 | t06-acceptance.txt |
| gen.py 示例 --profile schematic | 退出0；ERC0错0警；10网17器件一致；4目标pending | final-real.txt、final-real/passed-4ad41b5be5c54cd38e5011014bb45363/ |
| 无KiCad --profile generate | 退出0；ERC/netlist skipped；4目标pending | no-kicad-generate.txt |
| 无KiCad --profile schematic | 退出3；tool error；未生成图纸；4目标pending | no-kicad-schematic.txt |
| 报告、manifest逐文件SHA-256和长度核验 | 三份代表报告全部符合；27通过0失败 | verification-summary.json |
| git diff --check | 无diff错误，仅既有CRLF提示 | 工具运行记录 |

新增21测试覆盖目录损坏、不可读严格封装、缺工具、未知版本、工具超时、错误输入/设计、缺产物、ERC策略、旧网表防复用、隔离重复构建、确定性图纸、发布失败、输出不可写、stderr故障报告、共享快照和manifest哈希。原280测试保留。

### 文档与交接

新增docs/HANDOFF_T07.md；docs/HANDOFF.md顶部转到最新入口；README说明新命令、目录、范围及退出码迁移；DEEPSEEK_IMPLEMENTATION_GUIDE.md顶部更新实际契约，DEEPSEEK_START_PROMPT.md改为接手T08。DESIGN只加新架构权威说明，历史正文待T08整理。修正P1日志中不存在的output/p1-review-fix/t06-recheck/索引为output/t06-recheck/。

历史T06探针保留旧退出码，不覆盖原证据；新tests/t06_acceptance.py让stub版本正常响应并保留真实封装根目录，以保证半截报告确实到达ERC解析器，不能被版本/封装门禁提前拦截后冒充该负例通过。

### 尚未完成

T08独立验收、真实图纸可视化、DESIGN正文更新；性能/成本/PCB/热设计/实测仍未验证。示例±2%要求没有被降低。C3/C4耐压与4条禁用ERC检查仍明确列出。原P2诊断码严重级别一致性/迁移开闭区间未混入此任务。下一位AI从HANDOFF_T07.md与DEEPSEEK_START_PROMPT.md继续，不重做T00。
