"""IR 确定性校验 —— 把「规则」从提示词搬到代码里。

每一条诊断都带稳定错误码（编译器风格）。LLM 只根据 `错误码 + 字段路径 +
一句话` 定向修复，不重读整个 IR。severity=error 拦路；warning 可见不拦路。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ir.schema import (MODULE_TYPES, ROLES, SCHEMA_VERSION, TOPOLOGIES,
                       PowerIR)
from parts.partsdb import PARTS, Part, resolve_pin

#: role 与器件库 category 的对应关系。"passive" 是通配角色，不做类别检查。
ROLE_CATEGORY = {
    "regulator": "regulator",
    "inductor": "inductor",
    "capacitor": "capacitor",
    "resistor": "resistor",
    "diode": "diode",
    "connector": "connector",
}


@dataclass(frozen=True)
class Diagnostic:
    """一条校验诊断。path 是出错字段路径，如 "components[2].part"。"""
    code: str        # 稳定错误码，如 "E-IR-PARTS-003"
    severity: str    # "error" | "warning"
    path: str
    message: str     # 人读得懂、LLM 修得了的一句说明


@dataclass
class ValidateOutcome:
    """校验结论。ok 只当没有任何 error 时为真——继承 PcbResult.ok 哲学。"""
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(ir: PowerIR,
             parts: Mapping[str, Part] | None = None) -> ValidateOutcome:
    """全量校验，返回所有诊断（错误+警告）。`parts` 可注入以便测试。"""
    parts = PARTS if parts is None else parts
    diags: list[Diagnostic] = []
    _structure(ir, diags)
    _electrical(ir, diags)
    _components(ir, parts, diags)
    _nets(ir, parts, diags)
    _documentation(ir, diags)
    return ValidateOutcome(tuple(diags))


# ---- 结构 ----------------------------------------------------------------

def _structure(ir: PowerIR, diags: list[Diagnostic]) -> None:
    if ir.schema_version != SCHEMA_VERSION:
        diags.append(Diagnostic(
            "E-IR-STRUCT-001", "error", "schema_version",
            f"schema_version '{ir.schema_version}' 不受支持（当前 {SCHEMA_VERSION}）"))
    if not ir.project or not ir.project.replace("_", "").isalnum():
        diags.append(Diagnostic(
            "E-IR-STRUCT-002", "error", "project",
            f"project '{ir.project}' 不是合法标识符（字母数字下划线）"))
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
        if not (0 < r.min <= r.typ <= r.max):
            diags.append(Diagnostic(
                "E-IR-ELECT-001", "error", f"electrical.{name}",
                f"electrical.{name} 需满足 0 < min <= typ <= max"
                f"（当前 min={r.min} typ={r.typ} max={r.max}）"))
    if e.vout.max >= e.vin.min:
        diags.append(Diagnostic(
            "E-IR-ELECT-002", "error", "electrical",
            f"降压模块要求 vout.max < vin.min"
            f"（当前 vout.max={e.vout.max}，vin.min={e.vin.min}）"))
    if e.iout_max <= 0:
        diags.append(Diagnostic(
            "E-IR-ELECT-003", "error", "electrical.iout_max",
            f"iout_max 必须 > 0（当前 {e.iout_max}）"))
    # LDO 压差热耗近似 = (vin.typ - vout.typ) * iout_max，>2W 警告
    if ir.module_type == "power.ldo":
        pd = (e.vin.typ - e.vout.typ) * e.iout_max
        if pd > 2.0:
            diags.append(Diagnostic(
                "W-IR-ELECT-001", "warning", "electrical",
                f"LDO 压差 {e.vin.typ - e.vout.typ:.2f}V × {e.iout_max}A ≈ "
                f"{pd:.1f}W 热耗，请确认散热可行或改选 buck"))


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
    if (ir.module_type == "power.buck"
            and not any(c.role == "inductor" for c in ir.components)):
        diags.append(Diagnostic(
            "E-IR-PARTS-007", "error", "components",
            "buck 拓扑必须包含电感（role=inductor）"))


# ---- 网络 ----------------------------------------------------------------

def _nets(ir: PowerIR, parts: Mapping[str, Part],
          diags: list[Diagnostic]) -> None:
    names = [n.name for n in ir.nets]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        diags.append(Diagnostic(
            "E-IR-NETS-001", "error", "nets", f"网络名重复: {dupes}"))
    if not any(n.name == "GND" for n in ir.nets):
        diags.append(Diagnostic(
            "E-IR-NETS-002", "error", "nets", "缺少 GND 网络"))

    cmap = ir.component_map
    pinned: set[str] = set()
    for i, n in enumerate(ir.nets):
        base = f"nets[{i}]"
        if not n.name or " " in n.name:
            diags.append(Diagnostic(
                "E-IR-NETS-003", "error", f"{base}.name",
                f"网络名 '{n.name}' 非法（非空且不含空格）"))
        if not n.pins:
            diags.append(Diagnostic(
                "W-IR-NETS-001", "warning", f"{base}.pins",
                f"网络 '{n.name}' 没有引脚"))
        for p in n.pins:
            comp = cmap.get(p.ref)
            if comp is None:
                diags.append(Diagnostic(
                    "E-IR-NETS-004", "error", f"{base}.pins",
                    f"网络 '{n.name}' 引用了不存在的位号 '{p.ref}'"))
                continue
            part = parts.get(comp.part)
            if part is not None and resolve_pin(part, p.pin) is None:
                diags.append(Diagnostic(
                    "E-IR-PARTS-009", "error", f"{base}.pins",
                    f"网络 '{n.name}' 引用了位号 {p.ref} 不存在的引脚 "
                    f"'{p.pin}'（该器件引脚: {[r[0] for r in part.pins]}）"))
            pinned.add(p.ref)
    orphans = [c.ref for c in ir.components if c.ref not in pinned]
    if orphans:
        diags.append(Diagnostic(
            "W-IR-NETS-002", "warning", "nets",
            f"这些器件没接进任何网络（悬空）: {orphans}"))


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
