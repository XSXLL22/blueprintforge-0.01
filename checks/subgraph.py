"""必需外围子图检查：把 `Part.connection_rules` 跑成诊断。

规则定义在 `parts/rules.py`（纯数据），实例挂在器件上（partsdb），检查只有
这一份——校验器和生成器都从这里取结论。

判定用的网络拓扑来自 `checks.connectivity` 的唯一索引，规则不自己解析
IR 的 nets[]，避免出现第二套连接语义。**连接有冲突时不跑规则**：图本身
不可信时再报一堆"子图缺件"只是噪声，先修冲突。

四个反例子图（T01 顺延）对应这里：EN 直接接 VIN、自举电容接 GND、
缺 FREQ 电阻、COMP 并联 RC——它们在 M1 时全部能通过校验。
"""
from __future__ import annotations

from collections.abc import Mapping

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.partsdb import Part, resolve_pin
from parts.rules import CapBetween, NotTiedToPin, RToNet, SeriesRCToNet

from checks.connectivity import Connectivity


def _bridges(ir: PowerIR, parts: Mapping[str, Part], conn: Connectivity,
             net_a: str, net_b: str, category: str,
             exclude: str = "") -> list[str]:
    """跨接 net_a 与 net_b 的 category 类两脚器件位号。"""
    hits = []
    for comp in ir.components:
        if comp.ref == exclude:
            continue
        part = parts.get(comp.part)
        if part is None or part.category != category or len(part.pins) != 2:
            continue
        n0 = conn.net_of(comp.ref, part.pins[0].number)
        n1 = conn.net_of(comp.ref, part.pins[1].number)
        if {n0, n1} == {net_a, net_b}:
            hits.append(comp.ref)
    return hits


def _series_rc(ir: PowerIR, parts: Mapping[str, Part], conn: Connectivity,
               net: str, target: str) -> list[str]:
    """net —R—C→ target 的路径（可读证据）。"""
    paths = []
    for comp in ir.components:
        part = parts.get(comp.part)
        if part is None or part.category != "resistor" or len(part.pins) != 2:
            continue
        n0 = conn.net_of(comp.ref, part.pins[0].number)
        n1 = conn.net_of(comp.ref, part.pins[1].number)
        if net not in (n0, n1) or net == target:
            continue
        mid = n1 if n0 == net else n0
        if mid is None or mid in (net, target):
            continue
        for cap in _bridges(ir, parts, conn, mid, target, "capacitor",
                            exclude=comp.ref):
            paths.append(f"{net} —{comp.ref}→ {mid} —{cap}→ {target}")
    return paths


def check(ir: PowerIR, parts: Mapping[str, Part],
          conn: Connectivity) -> list[Diagnostic]:
    """跑所有器件的 connection_rules。"""
    if not conn.ok:
        return []
    diags: list[Diagnostic] = []
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None or not part.connection_rules:
            continue
        for rule in part.connection_rules:
            diags += _check_one(ir, parts, conn, i, comp.ref, part, rule)
    return diags


def _check_one(ir: PowerIR, parts: Mapping[str, Part], conn: Connectivity,
               index: int, ref: str, part: Part, rule) -> list[Diagnostic]:
    where = f"components[{index}]"

    def net_of_pin(pin: str) -> str | None:
        number = resolve_pin(part, pin)
        return conn.net_of(ref, number) if number is not None else None

    if isinstance(rule, CapBetween):
        net_a, net_b = net_of_pin(rule.pin_a), net_of_pin(rule.pin_b)
        if net_a is None or net_b is None:
            return []
        if _bridges(ir, parts, conn, net_a, net_b, "capacitor", exclude=ref):
            return []
        return [Diagnostic(
            "E-IR-NETS-008", "error", where,
            f"{ref}({part.name}) 的 {rule.pin_a}–{rule.pin_b} 之间缺少电容"
            f"（当前 {rule.pin_a} 在 '{net_a}'、{rule.pin_b} 在 '{net_b}'）",
            stage="connectivity", object_id=ref, rule_id=type(rule).__name__,
            evidence=rule.evidence,
            expected=f"一只跨接 {rule.pin_a}/{rule.pin_b} 的电容")]

    if isinstance(rule, RToNet):
        net = net_of_pin(rule.pin)
        if net is None:
            return []
        if _bridges(ir, parts, conn, net, rule.net, "resistor", exclude=ref):
            return []
        return [Diagnostic(
            "E-IR-NETS-008", "error", where,
            f"{ref}({part.name}) 的 {rule.pin} 没有经电阻接到 {rule.net}"
            f"（当前 {rule.pin} 在 '{net}'）",
            stage="connectivity", object_id=ref, rule_id=type(rule).__name__,
            evidence=rule.evidence, expected=f"{rule.pin} —R→ {rule.net}")]

    if isinstance(rule, SeriesRCToNet):
        net = net_of_pin(rule.pin)
        if net is None:
            return []
        if _series_rc(ir, parts, conn, net, rule.net):
            return []
        return [Diagnostic(
            "E-IR-NETS-008", "error", where,
            f"{ref}({part.name}) 的 {rule.pin} 缺少「电阻串联电容」到 "
            f"{rule.net} 的支路（当前 {rule.pin} 在 '{net}'）——"
            f"并联 RC 不是补偿网络",
            stage="connectivity", object_id=ref, rule_id=type(rule).__name__,
            evidence=rule.evidence, expected=f"{rule.pin} —R—C→ {rule.net}")]

    if isinstance(rule, NotTiedToPin):
        net, other = net_of_pin(rule.pin), net_of_pin(rule.other_pin)
        if net is None or other is None or net != other:
            return []
        return [Diagnostic(
            "E-IR-ELECT-004", "error", where,
            f"{ref}({part.name}) 的 {rule.pin} 与 {rule.other_pin} 同网"
            f"（'{net}'）：{rule.why}",
            stage="connectivity", object_id=f"{ref}.{rule.pin}",
            rule_id=type(rule).__name__, evidence=rule.evidence,
            expected=f"{rule.pin} 与 {rule.other_pin} 不同网")]

    return []
