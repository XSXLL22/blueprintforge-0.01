"""IR 确定性校验 —— 把「规则」从提示词搬到代码里。

每一条诊断都带稳定错误码（编译器风格）。LLM 只根据 `错误码 + 字段路径 +
一句话` 定向修复，不重读整个 IR。severity=error 拦路；warning 可见不拦路。
"""
from __future__ import annotations

from collections.abc import Mapping

from diagnostics import Diagnostic, Outcome
from ir import names
from ir.resolved import ResolvedIR, resolve
from ir.schema import (MODULE_TYPES, ROLES, SCHEMA_VERSION, TOPOLOGIES,
                       PowerIR, is_finite)
from parts.catalog import CatalogSnapshot, CatalogValidationPolicy
from parts.partsdb import Part

#: 兼容旧名：ValidateOutcome 就是通用的 Outcome。
ValidateOutcome = Outcome

#: role 与器件库 category 的对应关系。"passive" 是通配角色，不做类别检查。
ROLE_CATEGORY = {
    "regulator": "regulator",
    "inductor": "inductor",
    "capacitor": "capacitor",
    "resistor": "resistor",
    "diode": "diode",
    "connector": "connector",
}


def validate(ir: PowerIR,
             parts: Mapping[str, Part] | CatalogSnapshot | None = None,
             *, catalog_policy: CatalogValidationPolicy | None = None
             ) -> ValidateOutcome:
    """全量校验，返回所有诊断（错误+警告）。`parts` 可注入以便测试。

    目录自校验、连接、供电、子图与器件事实都取自 `resolve()` 那一份——
    生成器拒绝生成的理由就是这里报出来的，不在这里另写一遍（复核 P1#1：
    `CatalogSnapshot.validate()` 曾经只有"能调用"这一个身份）。

    `catalog_policy` 决定封装焊盘核对到什么程度（generate 口径记未核对，
    schematic 口径必须真核对）。校验器与生成器必须传同一个策略，否则
    「校验说通过、生成按另一个口径拒绝」这种事又会以新形式出现。
    """
    return validate_resolved(resolve(ir, parts, catalog_policy=catalog_policy))


def validate_resolved(rir: ResolvedIR) -> ValidateOutcome:
    """Validate the same resolved snapshot that generation and reconciliation use."""
    ir, catalog = rir.ir, rir.catalog
    diags: list[Diagnostic] = []
    _structure(ir, diags)
    _electrical(ir, diags)
    _targets(ir, diags)
    _components(ir, catalog.parts, diags)
    _nets(ir, catalog.parts, diags)

    diags.extend(rir.diagnostics)

    _documentation(ir, diags)
    return ValidateOutcome(tuple(diags),
                           unverified=rir.unverified + rir.catalog_unverified)


# ---- 结构 ----------------------------------------------------------------

def _structure(ir: PowerIR, diags: list[Diagnostic]) -> None:
    """语义层的防御性复核。

    语法规则只有一份（`ir/names.py`）——decode 在入口已经拦过一遍，
    这里复核是因为 `PowerIR` 也可以被直接构造（测试、工具、以后的其他
    前端），不能假设所有 PowerIR 都经过了 decode。
    """
    if ir.schema_version != SCHEMA_VERSION:
        diags.append(Diagnostic(
            "E-IR-STRUCT-001", "error", "schema_version",
            f"schema_version '{ir.schema_version}' 不受支持（当前 {SCHEMA_VERSION}）"))
    reason = names.project_name_error(ir.project)
    if reason is not None:
        diags.append(Diagnostic(
            "E-IR-STRUCT-002", "error", "project",
            f"project '{ir.project}' 不是合法标识符：{reason}"))
    if ir.module_type not in MODULE_TYPES:
        diags.append(Diagnostic(
            "E-IR-STRUCT-003", "error", "module_type",
            f"module_type '{ir.module_type}' 不在 {MODULE_TYPES} 中"))
        return
    if ir.topology not in TOPOLOGIES[ir.module_type]:
        diags.append(Diagnostic(
            "E-IR-STRUCT-004", "error", "topology",
            f"topology '{ir.topology}' 不适用于 {ir.module_type}"
            f"（允许 {TOPOLOGIES[ir.module_type]}）"))


# ---- 电气约束 ------------------------------------------------------------

def _electrical(ir: PowerIR, diags: list[Diagnostic]) -> None:
    e = ir.electrical
    for name, r in (("vin", e.vin), ("vout", e.vout)):
        # 有限性先查：NaN 参与的比较全为假，`0 < nan <= nan` 会"通过"
        # 下面每一条范围规则——先拦下来，否则所有判断都失去意义。
        if not all(is_finite(x) for x in (r.min, r.typ, r.max)):
            diags.append(Diagnostic(
                "E-IR-DECODE-004", "error", f"electrical.{name}",
                f"electrical.{name} 含非有限数值（NaN/Infinity）——"
                f"它会让所有比较为假，等价于「没有违规」，必须先修正输入",
                actual=f"min={r.min} typ={r.typ} max={r.max}"))
            continue
        if not (0 < r.min <= r.typ <= r.max):
            diags.append(Diagnostic(
                "E-IR-ELECT-001", "error", f"electrical.{name}",
                f"electrical.{name} 需满足 0 < min <= typ <= max"
                f"（当前 min={r.min} typ={r.typ} max={r.max}）"))
    if not is_finite(e.iout_max):
        diags.append(Diagnostic(
            "E-IR-DECODE-004", "error", "electrical.iout_max",
            f"iout_max 非有限（{e.iout_max}）——必须先修正输入",
            actual=str(e.iout_max)))
    elif e.iout_max <= 0:
        diags.append(Diagnostic(
            "E-IR-ELECT-003", "error", "electrical.iout_max",
            f"iout_max 必须 > 0（当前 {e.iout_max}）"))
    if (is_finite(e.vin.min) and is_finite(e.vout.max)
            and e.vout.max >= e.vin.min):
        diags.append(Diagnostic(
            "E-IR-ELECT-002", "error", "electrical",
            f"降压模块要求 vout.max < vin.min"
            f"（当前 vout.max={e.vout.max}，vin.min={e.vin.min}）"))
    # LDO 压差热耗近似 = (vin.typ - vout.typ) * iout_max，>2W 警告
    if ir.module_type == "power.ldo":
        pd = (e.vin.typ - e.vout.typ) * e.iout_max
        if pd > 2.0:
            diags.append(Diagnostic(
                "W-IR-ELECT-001", "warning", "electrical",
                f"LDO 压差 {e.vin.typ - e.vout.typ:.2f}V × {e.iout_max}A ≈ "
                f"{pd:.1f}W 热耗，请确认散热可行或改选 buck"))


# ---- 设计目标 ------------------------------------------------------------

def _targets(ir: PowerIR, diags: list[Diagnostic]) -> None:
    """目标值的防御性复核。

    结构层的严格解码已经查过一遍；这里再查是因为 `PowerIR` 可以被直接构造，
    而「比值目标写着 85（其实想写 85%）」这类错误一旦溜进来，后面的
    「是否满足目标」判断就会全部失真。
    """
    for t in ir.targets:
        if not is_finite(t.value):
            diags.append(Diagnostic(
                "E-IR-DECODE-004", "error", f"targets.{t.key}",
                f"目标 {t.key} 是非有限数值（{t.value}）——无法判定是否满足",
                object_id=t.key, actual=str(t.value)))
            continue
        if t.dimension == "ratio" and t.value > 1.0:
            diags.append(Diagnostic(
                "E-IR-DECODE-011", "error", f"targets.{t.key}",
                f"目标 {t.key} 是分数（0.02 = ±2%），{t.value} 超过 1.0——"
                f"若想写百分比请改成 0.85",
                object_id=t.key, actual=str(t.value), expected="≤ 1.0"))


# ---- 器件与市场 ----------------------------------------------------------

def _components(ir: PowerIR, parts: Mapping[str, Part],
                diags: list[Diagnostic]) -> None:
    refs = [c.ref for c in ir.components]
    dupes = sorted({r for r in refs if refs.count(r) > 1})
    if dupes:
        diags.append(Diagnostic(
            "E-IR-PARTS-001", "error", "components", f"位号重复: {dupes}"))

    for i, c in enumerate(ir.components):
        base = f"components[{i}]"
        if c.role not in ROLES:
            diags.append(Diagnostic(
                "E-IR-PARTS-002", "error", f"{base}.role",
                f"role '{c.role}' 不在 {ROLES} 中"))
        part = parts.get(c.part)
        if part is None:
            diags.append(Diagnostic(
                "E-IR-PARTS-003", "error", f"{base}.part",
                f"器件 '{c.part}' 不在器件库中——查无此料，不得凭空编型号"))
            continue
        if c.role in ROLE_CATEGORY and part.category != ROLE_CATEGORY[c.role]:
            diags.append(Diagnostic(
                "E-IR-PARTS-008", "error", f"{base}.role",
                f"位号 {c.ref} 的 role={c.role} 与器件库类别"
                f" '{part.category}' 不符"))
        if part.lifecycle == "eol":
            diags.append(Diagnostic(
                "E-IR-PARTS-004", "error", f"{base}.part",
                f"器件 '{c.part}' 已停产（EOL），必须换替代料："
                f"{part.alternatives or '无'}" ))
        elif part.lifecycle == "nrfnd":
            diags.append(Diagnostic(
                "W-IR-PARTS-001", "warning", f"{base}.part",
                f"器件 '{c.part}' 不建议新设计（NRFND）"))
        if part.stock is not None and part.stock <= 0:
            diags.append(Diagnostic(
                "E-IR-PARTS-005", "error", f"{base}.part",
                f"器件 '{c.part}' 无库存"))

    regs = [c.ref for c in ir.components if c.role == "regulator"]
    if len(regs) != 1:
        diags.append(Diagnostic(
            "E-IR-PARTS-006", "error", "components",
            f"电源模块必须恰好 1 个 regulator（当前 {len(regs)} 个: {regs}）"))
    # 拓扑必需器件（E-IR-PARTS-007）不在这一层判：那里要同时看 role、器件类别
    # 和具体拓扑（异步 buck 还必须有续流二极管），唯一实现是
    # `checks/parts.py::_topology`——校验器与生成器都从 `preflight` 取结论。
    # 这里曾有一份"buck 必须有电感"的弱化副本，会造成同一问题两个来源。


# ---- 网络 ----------------------------------------------------------------

def _nets(ir: PowerIR, parts: Mapping[str, Part],
          diags: list[Diagnostic]) -> None:
    """网络**本身**的检查（重名/缺 GND/名字非法/空网）。

    引脚层面的检查（不存在的位号/引脚、重复归属、必接脚、nc）在
    `checks/connectivity.py`——那里是引脚↔网络索引的唯一来源，生成器也用
    同一份，避免"校验一套、生成另一套"。
    """
    net_names = [n.name for n in ir.nets]
    dupes = sorted({n for n in net_names if net_names.count(n) > 1})
    if dupes:
        diags.append(Diagnostic(
            "E-IR-NETS-001", "error", "nets", f"网络名重复: {dupes}"))
    if not any(n.name == "GND" for n in ir.nets):
        diags.append(Diagnostic(
            "E-IR-NETS-002", "error", "nets", "缺少 GND 网络"))

    for i, n in enumerate(ir.nets):
        base = f"nets[{i}]"
        name_reason = names.net_name_error(n.name)
        if name_reason is not None:
            diags.append(Diagnostic(
                "E-IR-NETS-003", "error", f"{base}.name",
                f"网络名 '{n.name}' 非法：{name_reason}"))
        if not n.pins:
            diags.append(Diagnostic(
                "W-IR-NETS-001", "warning", f"{base}.pins",
                f"网络 '{n.name}' 没有引脚"))


# ---- 文档完整性 ----------------------------------------------------------

def _documentation(ir: PowerIR, diags: list[Diagnostic]) -> None:
    if not ir.design_rationale.strip():
        diags.append(Diagnostic(
            "W-IR-DOC-001", "warning", "design_rationale",
            "design_rationale 为空——没有设计理由的输出不可审查"))
    if not ir.assumptions:
        diags.append(Diagnostic(
            "W-IR-DOC-002", "warning", "assumptions",
            "assumptions 为空——应写明负载条件/环境温度/工作频率等假设"))
    if not ir.risks:
        diags.append(Diagnostic(
            "W-IR-DOC-003", "warning", "risks",
            "risks 为空——应标记哪些参数必须实测/复核"))
    # §4.1：未定义条件时不能给对应指标盖通过章。这里只提醒"缺条件"，
    # 「实测 vs 仿真 vs 未验证」的结论由 BuildReport（T07）给出。
    unverifiable = [t.key for t in ir.targets if not t.conditions]
    if unverifiable:
        diags.append(Diagnostic(
            "W-IR-DOC-004", "warning", "targets",
            f"这些目标没有定义验证条件（工况/带宽/温度/数量口径），"
            f"不满足条件就无法判定通过与否: {unverifiable}"))
