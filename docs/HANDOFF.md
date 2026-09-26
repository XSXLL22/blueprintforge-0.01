# 当前交接入口：T07 已完成，下一步 T08

最新交接与运行契约见 [HANDOFF_T07.md](HANDOFF_T07.md)。下面是 T06/P1 修复时的历史交接；“T07 未开始”、旧 gen 接口、旧退出码和旧证据路径不再描述当前版本。

---

# CircuitOS 首轮交接报告（T00–T06 完成，T07/T08 未开始）

**交接时点**：2026-09-26
**代码状态**：`HEAD = 4942bf8`（M1）**之上，全部改动尚未提交**
**交接时复跑**：`260 tests OK`（46.4s，0 fail 0 skip）｜ T06 验收探针 `26/26 通过` ｜
真实工程 ERC `0 违规`

> **交接之后追加（2026-09-26，同一轮工作内）**：按复核报告
> `docs/CODEX_REVIEW_T00_T06.md` 修掉两个 P1——目录自校验没进生产路径、
> catalog revision 漏字段。现在 **`280 tests OK`**。**读 §4.2/§4.3 前先看
> `docs/EXECUTION_LOG.md` 的「修复轮」一节**：那里的接口（`resolve(catalog_policy=…)`、
> `CatalogValidationPolicy`、`ResolvedIR.catalog_unverified`）取代了本文件写作时的
> 旧描述，T07 要按那节给的契约做。

> 本文件是交接入口。**逐任务的过程记录**（起点/问题/实现/验证/未完成/下一步）
> 在 `docs/EXECUTION_LOG.md`（852 行，T00–T06 各一节），那里是权威细节；
> 本文件只讲"现在是什么状态、怎么接下去"。

---

## 1. 一句话现状

首轮工作包 T00–T08 里的 **T00–T06 已完成并逐项留下可复跑的验证证据**；
**T07（pipeline/BuildReport/profile/退出码）与 T08（全量回归 + README/DESIGN + 交付报告）
未开始**。代码可以正常跑，但**一个提交都还没做**——这是当前最大的风险。

```
IR(JSON) ──decode/迁移──> PowerIR ──resolve(一次性)──> ResolvedIR
                                        │                  │
                                        ├─> checks/（连接·供电·子图·器件事实，判定只此一份）
                                        ├─> backend/kicad/schematic.py ──> .kicad_sch + 符号库 + .kicad_pro
                                        └─> backend/kicad/erc.py ──> ERC 报告（逐违规诊断）
                                            backend/kicad/netlist.py ──> 导出网表逐引脚对账
```

## 2. 本轮交付物

| 模块 | 行数 | 职责（一句话） |
|---|---|---|
| `diagnostics.py` | 197 | 诊断模型 + **错误码登记表**：新码不在 `CODES` 里就先报 `KeyError`，杜绝码义漂移 |
| `ir/decode.py` | 663 | 严格结构解析（类型/NaN/布尔/未知键/重复 key/空容器全拦） |
| `ir/migrate.py` | 166 | v0.1 → v0.2 显式迁移 |
| `ir/names.py` | 129 | 标识符语法（位号/网络名/工程名）的**唯一**来源 |
| `ir/resolved.py` | 136 | `ResolvedIR`：连接/供电/封装/额定值解析一次，生成与校验共用 |
| `checks/`（6 模块） | 961 | 连接解析、必接脚与显式 NC、供电来源证明、器件子图规则、器件事实核对 |
| `parts/catalog.py` | 273 | `CatalogSnapshot`：电气主数据与商业数据**分开哈希**；自校验 |
| `parts/rules.py` / `values.py` | 49 / 219 | 子图规则数据（带页码依据）／阻容感数值解析 |
| `backend/kicad/erc.py` | 506 | ERC 适配器：逐违规诊断、报告对应性（`E-ERC-003`）、登记制豁免 |
| `backend/kicad/netlist.py` | 397 | 导出网表解析 + **逐引脚双向对账**（生产链路，不是测试专用） |
| `backend/kicad/sexpr.py` / `footprint.py` | 160 / 156 | S 表达式解析（带引号/转义/嵌套）／封装焊盘读取 |
| `tests/`（9 文件 → 修复轮后 10 文件） | — | **260 例**（交接时）→ **280 例**（修复轮后，+`test_catalog.py` 20 例），含审查报告负例的回归化 |

**测试分布**：decode 66 ｜ erc 56 ｜ netlist_reconcile 31 ｜ connectivity 28 ｜
parts_facts 22 ｜ device_facts 20 ｜ **catalog 20** ｜ schematic 19 ｜ ir 16 ｜ partsdb 2

**证据**（都在 `output/`，共 267 个文件）：
`t00-evidence/`(24) `t01-evidence/`(10) `t02-evidence/`(17) `t03-evidence/`(43)
`t04-evidence/`(26) `t05-evidence/`(111) `t06-evidence/`(31)
其中 `tests/fixtures/erc/` 的 3 份报告是 **kicad-cli 真实产出**，用 sha256 钉住（含出处说明）。

## 3. 已证实的事实（交接时本次实跑，不是引用历史）

| 项 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `python -m unittest discover -s tests -t .` | **260 tests, OK**，退出码 0（修复轮后：**280 tests, OK** → `output/p1-review-fix/after-fix-tests.txt`） |
| T06 验收探针 | `python output/t06-evidence/t06_probes.py` | **26 项全通过**，退出码 0 → `output/t06-evidence/t06-probes-rerun.txt` |
| 真实生成链路 | `python gen.py examples/buck_12v_to_3v3.json -o output` | 退出码 0，ERC 0 错 0 警 + 网表 10 网络/17 器件与 IR 逐引脚一致 |

T06 的两个**实测**结论（跑了才知道，写进了 `tests/fixtures/erc/README.md`）：
1. `kicad-cli` 认 `.kicad_pro` 里的 `erc.rule_severities`（改 warning 就出 warning，改 ignore 就进 `ignored_checks`）。
2. **被禁用的检查会让"没违规"变成空话**：`pin_not_connected="ignore"` 且导线真断了时，该检查一声不吭，
   只剩 `power_pin_not_driven` 在报——这就是 `W-ERC-003` 存在的理由。

## 4. 未完成 / 未验证

### 4.1 未开始的任务

- **T07**：`pipeline/`（`models.py` / `build.py` / `workspace.py`）——profile（generate vs schematic）、
  按 build 分目录、`BuildReport` 落盘、退出码统一、`gen.py` 薄化。**我只做了接口调研，一行代码没写。**
- **T08**：全量回归 + README/DESIGN 更新 + 交付报告。

### 4.2 已知缺口（按主题汇总，逐条出处见 EXECUTION_LOG 各任务的「未完成」节）

- **`gen.py` 仍打印无范围的"全部通过"**——T06 后它至少要求 ERC 无错 + 网表一致，
  但仍不声明"性能/PCB 未验证"，T07 修（T03 未完成 6、T05 未完成 8 都指向这里）。
- **退出码语义仍是旧的**（现有 `3 = IR 结构不合格`、`4 = 对账失败`），与指南 §5.2 建议的
  `0/1/2/3/4`（成功／输入／产物／工具／IO）**含义不同**，T07 统一。
- **`ResolvedIR.unverified` 只被记录，没有出口**（示例当前 2 条：`C3` 的 COMP_RC 节点、`C4` 的 BST 节点——
  直流电压静态不可知，**没有**用估值凑"通过"）。`catalog.revision`、ERC 的 `ignored_checks` 同样只落在 stdout。T07 一并呈进报告。
  **修复轮补充**：目录层的未核对项现在单独记在 `ResolvedIR.catalog_unverified`（无封装读取器时
  每条带封装记录一条），`gen.py` 会打印并声明结论范围；**BuildReport 必须同时记 `catalog.id`
  （`builtin@<rev>`，名字+版本）与 `revision`**，`passed_profile` 不得暗示做过原理图级核对（报告 P1#1 要求 4）。
- **4 个设计目标全部未被计算**：`vout_accuracy` / `efficiency_min` / `ripple_max_v` / `bom_cost_max_cny`。
  没有仿真、没有实测、没有报价——T07 要把它们放进 pending 列表（不能删），T09/T11 才谈计算。
- **按网络角色的应力计算未做**（S8 的残留）：器件级额定值已核对，但"这个电容在这个位置承受什么电压"
  只做到两端直流电压近似，开关节点一律判不可知（T04 未完成 2/3/4/5）。
- **豁免清单是空的**（`backend/kicad/erc_allowlist.json` = `{"exemptions": []}`）：`W-ERC-001` 通路只在探针与测试里验过。
- **ERC 适配器的边界**（T06 未完成 3/4/5）：版本核对是字面比较（`10.0.6-rc1` 会误报）、
  新鲜度用本地时区、没有报告内容哈希（同一秒内换报告测不出）。
- **对账的边界**（T05 未完成 1/2/3）：只支持单页（层级直接拒）；只比连接，**不比 value、不比封装**。
- **README/DESIGN 仍是 T05 之前的口径**（还写着"34 测试全绿""网表 7 网"），按计划留到 T08 一次改完。

### 4.3 T07 的接口调研结论（未落代码，省得重新摸一遍）

- 需要的入口都已存在：`ir.decode.decode_text` → `Decoded(.draft/.diagnostics/.migrations/.usable)`；
  `ir.validate.validate(ir, parts, *, catalog_policy=…)` → `Outcome`；
  `ir.resolved.resolve(ir, parts, *, catalog_policy=…)` → `ResolvedIR`。
  **目录自校验（对应指南"输入/目录"那一项）只由 `resolve()` 调用一次**，校验器与生成器共用；
  策略是 `parts.catalog.CatalogValidationPolicy(pad_provider, require_pads)`——
  想核对封装就传 `gen.catalog_policy()`（按环境显式取），别再自己调 `CatalogSnapshot.validate()`，
  否则又会出现"两条路径两套口径"（修复轮 P1#1 的根因）。
- `backend/kicad/schematic.py::render_resolved(rir)` **已就绪**（T04 留的口子），但 `write_schematic()`
  当前签名是 `(ir, out_dir, parts)`——T07 要么加一个 `write_resolved(rir, out_dir)`，要么传 `rir.catalog.parts`，
  别绕过它自己拼路径。
- **改退出码会动测试**：`tests/test_netlist_reconcile.py` 里对账失败现在断言 `4`、工具失败断言 `2`，
  按 §5.2 应变成 `2` 与 `3`。改的依据是**指南 §5.2 的退出码表**（独立依据），
  不是为了变绿——请在改动处写明这条理由。
- **`review/reproduce_findings.py` 已与当前代码脱节**（它 `patch.object(gen, 'erc', ...)`，
  而 T06 后 `gen` 只有 `erc_mod`）。它是 T00 缺陷的历史复现记录，**不要重跑**——
  它会写 `review/probe-results.json`，那是只读的历史证据。

## 5. 接手怎么恢复

```bash
cd E:/deepseek/9-10
python -m unittest discover -s tests -t .        # 基线：280 tests OK（修复轮后）
python output/t06-evidence/t06_probes.py         # T06 验收：26/26
python output/p1-review-fix/verify_p1_fixed.py   # 修复轮：13/13（会失败的那种断言）
python gen.py examples/buck_12v_to_3v3.json -o output   # 真实链路：退出码 0
```

> `t06_probes.py` 会覆写 `output/t06-evidence/` 里的证据文件；只想复跑就拷到
> 同级目录（`output/t06-recheck/`）再跑，别动原证据。

**建议阅读顺序**：`docs/EXECUTION_LOG.md`（T00–T06 逐节 + 末尾「修复轮」一节，
比读代码快）→ `DEEPSEEK_IMPLEMENTATION_GUIDE.md` §5 + T07 节（下一个任务的
完整规格）→ 本文件。

环境：Python 3.14.7；KiCad CLI **10.0.6**
（`C:\Users\x\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe`，可用 `COS_KICAD_CLI` 覆盖）。

## 6. 硬约束（违反即返工，出自用户与指南）

1. **不得捏造**：器件型号、引脚、封装焊盘号、补偿参数、仿真模型、库存、实测数据——一律不许编。
2. 保留 IR → 确定性生成架构，**增量改，不整体重写**。
3. 不许用"删测试、放宽判据、批量忽略警告、自动补 NC/PWR_FLAG、降低指标"凑绿。
4. 验证没跑就标**未跑**；mock 结果与历史报告不得冒充本次结果；**未验证 ≠ 通过**。
5. **不自动 commit/push、不下单、不动硬件**；`review/` **只读**（`probe-results.json`、`baseline-tests.txt` 不得覆盖）。
6. 生产代码不得 `import tests.*`；对账这类双向逻辑必须在生产链路上（T05 已做到）。
7. ERC 逐违规处理：报告缺失/畸形/工具失败**绝不算零错误**。
8. 器件保证不了的需求，**保留需求不变**，报不符合项（示例的 ±2% 就是这样处理的，见 T01）。
9. 每个任务必须按指南 §11 更新 `docs/EXECUTION_LOG.md`。

## 7. 环境与坑（踩过的，别重踩）

- **Windows 控制台是 GBK**：脚本打印中文/箭头前必须
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，否则重定向出去是别人读不出的字节。
- **行尾**：仓库用 LF。用 Python heredoc 往返读写会把文件悄悄变成 CRLF（已发生过一次，导致 Edit 匹配失败）。
- **Git Bash heredoc 会吞反斜杠**：写含 `\` 的内容别走 heredoc。
- **只拷 `.kicad_sch` 跑 ERC 会得到 17 条 `lib_symbol_issues` 假告警**（缺符号库表），
  这不是电路问题——实验/探针一律整目录拷贝（T06 问题 3 有完整记录）。
- **网络受限**：`raw.githubusercontent.com` 不通，`git clone https://github.com/...` 可以；
  网络不通时**停下让用户开 VPN**，不许自己改代理/网络配置。

## 8. 风险

1. **全部改动未提交**（HEAD 仍是 4942bf8，M1）：9 个已跟踪文件 +1259/−380，26 个未跟踪条目
   （24 个新 Python 模块约 6.5k 行 + `tests/fixtures/`、`docs/`、`review/`）。
   **一次 `git clean -fdx` 就会清掉 T00–T06 的全部新模块和历史审查材料。** 建议尽快分批提交。
2. `review/` 与 `output/t*-evidence/` 都是**未跟踪**的——同样暴露在 `git clean` 下。
3. 器件目录里多数料仍是 `review_state="generic"` 的泛化料号，**未落到可采购 MPN**（不影响软件结论，但别当成 BOM）。

## 9. 本轮范围外的另一件事：grill-me skill

用户要求给 Claude Code 与 Codex 都装 `grill-me`（= 之前口误的 "girllme"）。已装完并验过：

| 位置 | 内容 |
|---|---|
| `C:\Users\x\.claude\skills\` | `grill-me/`（壳：`disable-model-invocation: true`，只能手动 `/grill-me`）+ `grilling/`（真正的访谈算法） |
| `C:\Users\x\.codex\skills\` | 同上，两个都装 |

两处文件清单一致，两个 `SKILL.md` 的 sha256 与源仓库（`mattpocock/skills`）逐字节相同。
**新开会话才生效**。同仓库还有个 `grill-with-docs`（多依赖一个 `domain-modeling`），**未装**。
