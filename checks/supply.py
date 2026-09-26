"""供电来源证明：哪些网络**有依据**被驱动，哪些只是被声明成 power 类。

为什么需要它：KiCad 的 ERC 只问「这个网有没有人驱动」，而生成器可以让它
闭嘴——给任何网络插一个 PWR_FLAG 就行。于是「电源网没人供电」这种致命
缺陷会被自己的输出掩盖掉。所以顺序反过来：

    1. 先证明来源（本模块）——证明不了就是 IR 错误，不生成；
    2. 再给**已证明**但没有 power_out 引脚的网络加 PWR_FLAG。

证据只有三类，都要能指给人看：

    power_out 引脚     器件资料里明确输出功率的脚（如 U1.1 SW）
    外部端口            IR 的 ports[] 里 drives=true 的声明（模块边界外的供电）
    串联无源元件路径     经电感/电阻从**已证明**的网络引过来（如 SW→L1→VOUT）

`netclass = "power"` **不是**证据：它是用户的分类，不是供电事实。反过来，
把 netclass 改成 signal 也躲不掉——只要网络里有 power_in 引脚就必须有来源。

串联路径只认电感与电阻：电容隔直（不能作为供电路径），二极管方向相关
（反向就是断路），二者要作为供电来源必须由 ports[] 显式声明。宁可要求
设计者补一条声明，也不要靠猜测把没有来源的网络判成有来源。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.partsdb import PARTS, Part

from checks.connectivity import Connectivity

#: 可以作为串联供电路径的器件类别（不含电容/二极管，理由见模块文档）。
SERIES_CATEGORIES = ("inductor", "resistor")


@dataclass(frozen=True)
class Supply:
    """供电来源的证明结果。"""
    proof: Mapping[str, str]          # 网络 → 一句话证据
    flagged: frozenset[str]           # 需要 PWR_FLAG 的网络（已证明、无 power_out）
    needing: frozenset[str]           # 需要证明来源的网络（power 类或含 power_in）
    unproven: frozenset[str]          # needs - proof


def needs_drive(ir: PowerIR, parts: Mapping[str, Part],
                conn: Connectivity) -> set[str]:
    """需要证明来源的网络：**含 power_in 引脚**或声明为 power 类。

    用引脚电气类型而不是只信 netclass：用户把 netclass 从 power 改掉不该
    改变"这个网需要供电"这个事实。
    """
    out: set[str] = {n.name for n in ir.nets if n.netclass == "power"}
    for net in ir.nets:
        for token in conn.net_pins.get(net.name, ()):
            ref, _, number = token.partition(".")
            comp = ir.component_map.get(ref)
            part = parts.get(comp.part) if comp else None
            if part is None:
                continue
            pin = part.pin(number)
            if pin is not None and pin.kind == "power_in":
                out.add(net.name)
    return out


def _power_out_nets(ir: PowerIR, parts: Mapping[str, Part],
                    conn: Connectivity) -> dict[str, str]:
    """有 power_out 引脚的网络 → 证据文本。"""
    out: dict[str, str] = {}
    for net in ir.nets:
        for token in conn.net_pins.get(net.name, ()):
            ref, _, number = token.partition(".")
            comp = ir.component_map.get(ref)
            part = parts.get(comp.part) if comp else None
            if part is None:
                continue
            pin = part.pin(number)
            if pin is not None and pin.kind == "power_out":
                out.setdefault(
                    net.name,
                    f"{ref}.{number}({pin.name or number}) 是 {part.name} 的 "
                    f"power_out 引脚")
    return out


def _series_links(ir: PowerIR, parts: Mapping[str, Part],
                  conn: Connectivity) -> list[tuple[str, str, str]]:
    """串联无源元件的两端（网络A, 网络B, 证据文本），只收两脚器件。"""
    links: list[tuple[str, str, str]] = []
    for comp in ir.components:
        part = parts.get(comp.part)
        if part is None or part.category not in SERIES_CATEGORIES:
            continue
        numbers = [p.number for p in part.pins]
        if len(numbers) != 2:
            continue
        a = conn.net_of(comp.ref, numbers[0])
        b = conn.net_of(comp.ref, numbers[1])
        if a is None or b is None or a == b:
            continue
        links.append((a, b, f"经 {comp.ref}({part.name}) 由 {a} 引来"))
        links.append((b, a, f"经 {comp.ref}({part.name}) 由 {b} 引来"))
    return links


def prove(ir: PowerIR, parts: Mapping[str, Part] | None = None,
          conn: Connectivity | None = None) -> tuple[Supply, list[Diagnostic]]:
    """证明每个需要供电的网络都有来源；返回结论与诊断。"""
    parts = PARTS if parts is None else parts
    if conn is None:
        from checks.connectivity import resolve
        conn, _ = resolve(ir, parts)

    diags: list[Diagnostic] = []
    needing = needs_drive(ir, parts, conn)

    # 外部端口：声明 drives=true 的网络
    proof: dict[str, str] = {}
    seen_ports: dict[str, int] = {}
    for i, port in enumerate(ir.ports):
        path = f"ports[{i}]"
        seen_ports[port.name] = seen_ports.get(port.name, 0) + 1
        if port.net not in {n.name for n in ir.nets}:
            diags.append(Diagnostic(
                "E-IR-NETS-006", "error", f"{path}.net",
                f"端口 '{port.name}' 声明挂在网络 '{port.net}' 上，"
                f"但 IR 里没有这个网络——端口声明与连接事实冲突",
                stage="supply", object_id=port.name,
                actual=port.net, expected="已有的网络名"))
            continue
        if port.drives:
            proof.setdefault(port.net,
                             f"外部端口 {port.name}（direction={port.direction}）"
                             f"声明由模块外部驱动")
    for name, count in seen_ports.items():
        if count > 1:
            diags.append(Diagnostic(
                "E-IR-NETS-006", "error", "ports",
                f"端口名 '{name}' 重复声明 {count} 次——端口是模块边界，"
                f"同名端口无法区分是哪一个", stage="supply", object_id=name,
                actual=str(count), expected="1"))

    # power_out 引脚
    proof.update(_power_out_nets(ir, parts, conn))

    # 串联无源路径：从已证明的网络做闭包（不用递归，避免环）
    links = _series_links(ir, parts, conn)
    changed = True
    while changed:
        changed = False
        for a, b, why in links:
            if a in proof and b not in proof and b in needing:
                proof[b] = why
                changed = True

    unproven = needing - set(proof)
    for net_name in sorted(unproven):
        index = next(i for i, n in enumerate(ir.nets) if n.name == net_name)
        diags.append(Diagnostic(
            "E-IR-NETS-005", "error", f"nets[{index}]",
            f"电源网络 '{net_name}' 没有可证明的来源：既没有 power_out 引脚，"
            f"也没有 drives=true 的外部端口，也没有从已证明网络引来的串联"
            f"无源元件。生成器不会为它补 PWR_FLAG——那只会让 ERC 闭嘴，"
            f"不能证明它有电", stage="supply", object_id=net_name,
            expected="power_out 引脚 / 外部端口声明 / 串联路径之一"))

    # 需要 PWR_FLAG 的网络：已证明但没有 power_out 引脚
    powered = set(_power_out_nets(ir, parts, conn))
    flagged = frozenset(n for n in needing if n in proof and n not in powered)

    supply = Supply(proof=dict(proof), flagged=flagged, needing=frozenset(needing),
                    unproven=frozenset(unproven))
    return supply, diags
