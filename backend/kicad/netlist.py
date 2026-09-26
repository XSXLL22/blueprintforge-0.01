"""导出网表的解析与**逐引脚双向对账**（T05）。

为什么必须有这一步：`gen.py` 之前的"全部通过"只表示"ERC 没报错"。ERC 看的是
**原理图自己**——符号被解析成什么、导线连到哪，它对 IR 一无所知。原理图生成
器把网画错（漏一根线、两个网络并成一个、引脚接到隔壁网络）时，ERC 完全可能
一条不报。对账是唯一能证明"画出来的这张图 == IR 描述的那张图"的环节。

两个方向都要查（只查一个方向会漏一半）：

    IR → 网表：IR 声明的网络/引脚在导出结果里**存在**且**落在同一个网**上
    网表 → IR：导出结果里没有 IR 不知道的网络/器件/引脚

**不猜**：
- 解析用 kicad-cli 的 `kicadxml`（标准 XML，标准库解析），不用正则——
  XML 里的转义、嵌套、属性顺序都不是正则能可靠处理的；本工程的
  `kicadsexpr`/`.net` 格式同理走 `sexpr.py`。
- 网络名归一化只做**一件确定的事**：根图纸的局部标签在网表里叫 `/NAME`，
  与 IR 里的 `NAME` 是同一个网。除此之外不接受别的差异；两个不同网名归一化
  后撞在一起、或出现层级路径（`/sub/NAME`）时**立即失败**，不猜映射。
- 元素个数**先数后比**：重复出现的网络名/引脚记录会被数出来并报错，
  不会先 `set()` 掉、把"导出结果是坏的"伪装成"集合一致"。

错误码沿用 `diagnostics.CODES` 里已登记的 `E-NET-001..005`（定义见那里），
只新增 `E-NET-006`（层级/多页网表不受支持）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from diagnostics import Diagnostic
from ir.schema import PowerIR
from ir.resolved import ResolvedIR, resolve
from parts.partsdb import Part

from backend.kicad import sexpr


#: 本生成器使用的**官方**电源符号库 id（另一类是自绘的 circuitos:PWR_<网络>）。
AUX_LIB_IDS = frozenset({"power:GND", "power:PWR_FLAG"})


class NetlistError(ValueError):
    """网表文件本身不可用（不存在/空/截断/结构不符）。"""


@dataclass(frozen=True)
class Node:
    """网表里的一个引脚节点。`pin` 是**物理脚号**（封装焊盘号）。"""

    ref: str
    pin: str
    pinfunction: str = ""
    pintype: str = ""


@dataclass(frozen=True)
class ExportedComponent:
    ref: str
    value: str = ""
    footprint: str = ""
    libsource: str = ""


@dataclass(frozen=True)
class ExportedNet:
    code: str
    name: str
    #: 原样保留，含重复项——重复由 `node_counts` 数出来
    nodes: tuple[Node, ...] = ()

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(n.ref for n in self.nodes)


@dataclass(frozen=True)
class Netlist:
    """一次真实导出的解析结果。**保留计数**，不提前去重。"""

    source: str
    tool: str = ""
    components: tuple[ExportedComponent, ...] = ()
    nets: tuple[ExportedNet, ...] = ()
    #: 同一网络名出现次数（>1 = 导出结果自相矛盾）
    net_name_counts: Mapping[str, int] = field(default_factory=dict)
    #: 同一 (ref, pin) 在全表出现次数（>1 = 重复记录）
    node_counts: Mapping[tuple[str, str], int] = field(default_factory=dict)
    #: 层级路径（非根图纸）——本策略不支持，见到就拒
    hierarchical: tuple[str, ...] = ()

    def component(self, ref: str) -> ExportedComponent | None:
        for c in self.components:
            if c.ref == ref:
                return c
        return None

    def net(self, name: str) -> ExportedNet | None:
        for n in self.nets:
            if n.name == name:
                return n
        return None


# ---- 解析 -------------------------------------------------------------------

def parse_kicadxml(text: str, source: str = "<text>") -> Netlist:
    """解析 kicad-cli `--format kicadxml` 的输出。

    结构不符、内容为空、被截断都抛 `NetlistError`——**没有内容不等于零违规**，
    也不等于"对账通过"。
    """
    if not text.strip():
        raise NetlistError(f"{source}: 网表文件为空")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NetlistError(f"{source}: XML 无法解析（{exc}）——"
                           f"文件可能被截断或是别的工具的输出") from exc
    if root.tag != "export":
        raise NetlistError(f"{source}: 顶层元素是 <{root.tag}>，期望 <export>")
    tool = (root.findtext("design/tool") or "").strip()
    comps_el, nets_el = root.find("components"), root.find("nets")
    if comps_el is None or nets_el is None:
        raise NetlistError(
            f"{source}: 缺少 <components> 或 <nets> 段（"
            f"实际顶层子元素 {[c.tag for c in root]}）")

    hierarchical = _hierarchical_paths(root, nets_el)

    components = []
    for c in comps_el.findall("comp"):
        ref = (c.get("ref") or "").strip()
        if not ref:
            raise NetlistError(f"{source}: <comp> 缺 ref 属性")
        components.append(ExportedComponent(
            ref=ref,
            value=(c.findtext("value") or ""),
            footprint=(c.findtext("footprint") or ""),
            libsource=(c.findtext("libsource") or "")))

    nets, names = [], []
    for n in nets_el.findall("net"):
        name = n.get("name")
        if name is None:
            raise NetlistError(f"{source}: <net> 缺 name 属性")
        names.append(name)
        nodes = []
        for node in n.findall("node"):
            ref, pin = (node.get("ref") or "").strip(), (node.get("pin") or "").strip()
            if not ref or not pin:
                raise NetlistError(
                    f"{source}: 网络 {name!r} 的 <node> 缺 ref/pin 属性")
            nodes.append(Node(ref=ref, pin=pin,
                              pinfunction=(node.get("pinfunction") or ""),
                              pintype=(node.get("pintype") or "")))
        nets.append(ExportedNet(code=(n.get("code") or ""), name=name,
                                nodes=tuple(nodes)))

    if not nets:
        raise NetlistError(f"{source}: 网表里没有任何 <net>——"
                           "导出结果为空或不完整，不能当作「没有差异」")
    if not components:
        raise NetlistError(f"{source}: 网表里没有任何 <comp>")

    node_counts = Counter((nd.ref, nd.pin) for n in nets for nd in n.nodes)
    return Netlist(source=source, tool=tool, components=tuple(components),
                   nets=tuple(nets), net_name_counts=Counter(names),
                   node_counts=node_counts, hierarchical=hierarchical)


def _hierarchical_paths(root: ET.Element, nets_el: ET.Element) -> tuple[str, ...]:
    """层级图纸的证据：`<sheet>` 的 name 不是根，或网名里有多级路径。"""
    bad: list[str] = []
    for sheet in root.findall("design/sheet"):
        name = (sheet.get("name") or "").strip()
        if name and name != "/":
            bad.append(f"sheet {name}")
    for net in nets_el.findall("net"):
        name = net.get("name") or ""
        if "/" in name:
            # 根图纸的局部标签是 "/NAME"；再出现斜杠就是层级路径
            stripped = name[1:] if name.startswith("/") else name
            if "/" in stripped:
                bad.append(f"net {name}")
    return tuple(sorted(set(bad)))


def read_netlist(path: Path) -> tuple[Netlist | None, list[Diagnostic]]:
    """读文件 → 解析。失败**只**返回诊断，不抛异常，也不返回半个网表。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [Diagnostic(
            "E-NET-001", "error", str(path),
            f"网表文件读不到：{exc}——没有报告不等于没有错误",
            stage="netlist", object_id=str(path))]
    try:
        return parse_kicadxml(text, source=str(path)), []
    except NetlistError as exc:
        return None, [Diagnostic(
            "E-NET-001", "error", str(path), str(exc),
            stage="netlist", object_id=str(path))]


# ---- 网络名归一化 -----------------------------------------------------------

def normalize_net_name(raw: str) -> str:
    """网表里的网络名 → IR 里的网络名。

    当前生成策略是**单页原理图**：信号网用根图纸局部标签（导出成 `/NAME`），
    电源网用电源符号（导出成裸名 `NAME`）。所以归一化只剥掉**恰好一个**
    前导斜杠——不是 `lstrip("/")`：后者会把 `/a/b` 变成 `a/b`、把 `//X`
    和 `X` 并成一个，正好掩盖层级与歧义。
    """
    return raw[1:] if raw.startswith("/") else raw


def _normalized_map(netlist: Netlist) -> tuple[dict[str, list[ExportedNet]],
                                                list[Diagnostic]]:
    """归一化网络名 → 网表网络列表。歧义在这里就被拦下。"""
    diags: list[Diagnostic] = []
    if netlist.hierarchical:
        diags.append(Diagnostic(
            "E-NET-006", "error", netlist.source,
            f"网表来自层级/多页原理图（{list(netlist.hierarchical)}）——"
            f"当前生成策略是单页，拒绝猜测层级到 IR 网络的映射",
            stage="netlist", object_id=", ".join(netlist.hierarchical)))
    by_name: dict[str, list[ExportedNet]] = {}
    for net in netlist.nets:
        by_name.setdefault(normalize_net_name(net.name), []).append(net)
    for name, group in sorted(by_name.items()):
        if len(group) > 1:
            diags.append(Diagnostic(
                "E-NET-002", "error", netlist.source,
                f"网表里有 {len(group)} 个网络归一化后同名 '{name}'"
                f"（原名 {[n.name for n in group]}）——名字会碰撞，"
                f"不猜哪个是 IR 的那个网",
                stage="netlist", object_id=name,
                actual=[n.name for n in group]))
    for name, count in sorted(netlist.net_name_counts.items()):
        if count > 1:
            diags.append(Diagnostic(
                "E-NET-003", "error", netlist.source,
                f"网络名 '{name}' 在网表里出现 {count} 次——"
                f"导出结果自相矛盾",
                stage="netlist", object_id=name, actual=count))
    return by_name, diags


# ---- 对账 -------------------------------------------------------------------

def reconcile(ir: PowerIR, parts: Mapping[str, Part] | None,
              netlist: Netlist, *, aux_refs: Iterable[str] = ()
              ) -> list[Diagnostic]:
    """`reconcile_resolved` 的便捷入口（与生成器的 render/render_resolved 同型）。"""
    return reconcile_resolved(resolve(ir, parts), netlist, aux_refs=aux_refs)


def reconcile_resolved(rir: ResolvedIR, netlist: Netlist,
                       *, aux_refs: Iterable[str] = ()) -> list[Diagnostic]:
    """IR ↔ 导出网表的逐引脚双向对账。

    `aux_refs` 是**本次生成器真正画出来的**辅助符号位号（电源符号、PWR_FLAG）。
    它们若被导出，按这张确定的清单排除；清单之外的多余器件一律报
    `E-NET-005`——不按 `#` 之类的前缀过滤，那会把用户器件一起吞掉。
    """
    aux = set(aux_refs)
    conn = rir.connectivity
    diags: list[Diagnostic] = []

    by_name, name_diags = _normalized_map(netlist)
    diags.extend(name_diags)

    # 重复记录：同一只脚在整张网表里出现多次
    for (ref, pin), count in sorted(netlist.node_counts.items()):
        if count > 1:
            diags.append(Diagnostic(
                "E-NET-003", "error", netlist.source,
                f"{ref}.{pin} 在网表里出现 {count} 次——"
                f"同一只脚只能属于一个网络",
                stage="netlist", object_id=f"{ref}.{pin}", actual=count))

    # 网表 → IR：器件与引脚是否存在
    known_refs = {c.ref for c in rir.components}
    unknown_refs: set[str] = set()
    for comp in netlist.components:
        if comp.ref in aux or comp.ref in known_refs:
            continue
        unknown_refs.add(comp.ref)
        diags.append(Diagnostic(
            "E-NET-005", "error", netlist.source,
            f"网表里出现 IR 未声明的器件 '{comp.ref}'"
            f"（value={comp.value!r}）——它不是本次生成的辅助符号"
            f"（辅助符号清单：{sorted(aux) or '空'}）",
            stage="netlist", object_id=comp.ref, actual=comp.value))

    # 网表 → IR：逐引脚。这里**不跳过重名网络**——重名本身由 E-NET-002 报，
    # 但"这只脚落在了一个名字就不对的网上"是独立的事实，被重名盖掉会让
    # 报告少说一半真话。
    for net in netlist.nets:
        name = normalize_net_name(net.name)
        for node in net.nodes:
            ref, pin = node.ref, node.pin
            if ref in aux or ref in unknown_refs:
                continue                  # 辅助符号 / 未知器件已报，不重复报
            part = rir.part_of(ref)
            if part is None:
                continue
            if part.pin(pin) is None:
                diags.append(Diagnostic(
                    "E-NET-005", "error", netlist.source,
                    f"网表里 {ref}.{pin} 落在网络 '{name}'，但 {part.name} "
                    f"没有这个引脚——网表里多出来一只脚",
                    stage="netlist", object_id=f"{ref}.{pin}", actual=pin))
                continue
            expected = conn.net_of(ref, pin)
            if expected is None:
                diags.append(Diagnostic(
                    "E-NET-005", "error", netlist.source,
                    f"网表里 {ref}.{pin} 落在网络 '{name}'，但 IR 没有把它"
                    f"接进任何网络",
                    stage="netlist", object_id=f"{ref}.{pin}", actual=name))
            elif expected != name:
                diags.append(Diagnostic(
                    "E-NET-004", "error", netlist.source,
                    f"{ref}.{pin} 在网表里属于网络 '{name}'，IR 里属于 "
                    f"'{expected}'",
                    stage="netlist", object_id=f"{ref}.{pin}",
                    actual=name, expected=expected))

    # IR → 网表：应存在的网络与引脚
    expected_nets = {n.name for n in rir.ir.nets}
    got_nets = set(by_name)
    for name in sorted(expected_nets - got_nets):
        diags.append(Diagnostic(
            "E-NET-002", "error", netlist.source,
            f"IR 声明的网络 '{name}' 在导出网表里不存在"
            f"（网表里的网络：{sorted(got_nets)}）",
            stage="netlist", object_id=name, expected=name))
    for name in sorted(got_nets - expected_nets):
        nets = by_name[name]
        refs = [f"{n.ref}.{n.pin}" for net in nets for n in net.nodes]
        diags.append(Diagnostic(
            "E-NET-002", "error", netlist.source,
            f"网表里有 IR 未声明的网络 '{name}'（原名 "
            f"{[n.name for n in nets]}，引脚 {refs}）",
            stage="netlist", object_id=name, actual=name))

    # 「IR 里的每只脚都出现过」按**引脚集合**判，不按网络名——否则重名网络
    # 里的脚会被误报成"少画了"，把一种错误说成另一种。
    exported_pins = {(nd.ref, nd.pin) for net in netlist.nets for nd in net.nodes}
    for (ref, pin), net in sorted(conn.pin_net.items()):
        if ref not in known_refs or (ref, pin) in exported_pins:
            continue
        diags.append(Diagnostic(
            "E-NET-003", "error", netlist.source,
            f"IR 里 {ref}.{pin} 属于网络 '{net}'，导出网表里没有这只脚"
            f"——图上少画了/少连了",
            stage="netlist", object_id=f"{ref}.{pin}",
            actual=None, expected=net))

    return diags


# ---- 辅助符号：从**本次生成的**原理图里取确定清单 ----------------------------

def auxiliary_refs(schematic_text: str) -> dict[str, str]:
    """本次生成器画出的辅助符号位号 → lib_id。

    判定用 **lib_id**：本生成器画的辅助符号只有三种——`power:GND`、
    `power:PWR_FLAG`、自绘的 `circuitos:PWR_<网络>`。不用"位号以 # 开头"
    这类宽泛前缀：用户的器件也可能叫 `#PWR001`，而库里的 `#PWR`/`#FLG`
    只是隐藏模板（`lib_symbols` 段里的定义），两者都不该被当成辅助符号
    悄悄排除掉。
    """
    out: dict[str, str] = {}
    roots = sexpr.parse(schematic_text)
    if len(roots) != 1 or not roots[0] or roots[0][0] != "kicad_sch":
        raise NetlistError("不是本生成器产出的 .kicad_sch（顶层不是 kicad_sch）")
    # lib_symbols 里也有 `(symbol ...)`（库定义，Reference 是 "#PWR" 这类
    # 模板）——只取 kicad_sch 的直接子元素即实例，天然排除库定义。
    for node in sexpr.children(roots[0], "symbol"):
        libs = sexpr.values_of(node, "lib_id")
        if not libs:
            continue
        lib_id = str(libs[0])
        if not (lib_id in AUX_LIB_IDS or lib_id.startswith("circuitos:PWR_")):
            continue
        for prop in sexpr.children(node, "property"):
            if len(prop) >= 3 and prop[1] == "Reference":
                out[str(prop[2])] = lib_id
                break
    return out
