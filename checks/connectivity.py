"""统一连接解析：IR 的 nets[] / components[].nc → 唯一的引脚↔网络索引。

为什么必须**只有一处**解析：

- 校验器与生成器各解析一次，就会出现「校验通过、生成的却是另一回事」——
  S3 类假通过的结构性来源；
- 逐条就地解析会出现「后值覆盖」：同一物理引脚被两个网络引用时，最后写入
  的那条静默获胜。T00 的 probe `duplicate_pin_across_nets` 观察到的
  `{"ok": true, "rendered_U1_pin6_net": "GND"}` 正是这个行为。

所以本模块先把**全部**声明收齐（claims），再统一判冲突。判定结果与
nets[] 的书写顺序无关，也与「引脚写功能名还是写物理号」无关——两者先
归一化成物理脚号再比较，所以 `{"pin": "6"}` 与 `{"pin": "FREQ"}` 指向同一只脚
这件事不会被绕过。

错误码沿用语义层**已有**的码（码义只有一份，见 diagnostics.CODES）：

    E-IR-PARTS-009  引用了器件不存在的引脚
    E-IR-PARTS-010  同一物理引脚重复归属多个网络
    E-IR-PARTS-011  必接引脚没有接入任何网络
    E-IR-PARTS-012  非法的不连接声明（器件不允许悬空/未声明）
    E-IR-PARTS-013  同一引脚既被连接又被声明不连接
    E-IR-PARTS-017  引脚引用有歧义（脚名与脚号指向不同引脚）
    E-IR-NETS-004   网络引用了不存在的位号
    E-IR-NETS-009   同一引脚在同一网络里重复出现
    W-IR-NETS-002   器件没有接进任何网络
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.partsdb import PARTS, Part


@dataclass(frozen=True)
class Claim:
    """一条「某引脚属于某网络」的声明（未经冲突判定的原始事实）。"""
    ref: str
    number: str      # 物理脚号
    net: str
    path: str        # 诊断定位，如 nets[3].pins[1].pin


@dataclass(frozen=True)
class Connectivity:
    """解析结果：引脚↔网络的唯一事实来源。

    `pin_net` 只包含**无冲突**的引脚；有冲突的引脚不会带着"最后一个赢家"
    混进来——冲突本身已经在 `diagnostics` 里报错。
    """
    pin_net: Mapping[tuple[str, str], str]
    net_pins: Mapping[str, tuple[str, ...]]     # 网络 → ("U1.7", "C1.1", …)
    nc: frozenset[tuple[str, str]]
    claims: tuple[Claim, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ok(self) -> bool:
        return not any(d.is_error for d in self.diagnostics)

    def net_of(self, ref: str, number: str) -> str | None:
        return self.pin_net.get((ref, number))

    def is_nc(self, ref: str, number: str) -> bool:
        return (ref, number) in self.nc

    def nets_of(self, ref: str) -> tuple[str, ...]:
        return tuple(sorted({n for (r, _), n in self.pin_net.items() if r == ref}))

    def render(self) -> str:
        return "\n".join(d.render() for d in self.diagnostics)


def resolve(ir: PowerIR,
            parts: Mapping[str, Part] | None = None
            ) -> tuple[Connectivity, list[Diagnostic]]:
    """解析 IR 的全部连接声明；返回索引与诊断（诊断里含尚未成立的冲突）。"""
    parts = PARTS if parts is None else parts
    diags: list[Diagnostic] = []
    claims: list[Claim] = []
    nc: set[tuple[str, str]] = set()

    cmap = ir.component_map

    def part_of(ref: str) -> Part | None:
        comp = cmap.get(ref)
        return None if comp is None else parts.get(comp.part)

    def locate(ref: str, pin: str, path: str) -> str | None:
        """引脚引用 → 物理脚号。查不动时给出结构化诊断并返回 None。"""
        part = part_of(ref)
        if part is None:
            return None          # 位号/器件缺失已由 _components/_nets 报过
        hits = part.pin_ambiguity(pin)
        if hits:
            diags.append(Diagnostic(
                "E-IR-PARTS-017", "error", path,
                f"引脚引用 '{pin}' 在 {part.name} 里指向多个不同引脚 {list(hits)}"
                f"——必须写物理脚号才能确定是哪个", stage="connectivity",
                object_id=f"{ref}.{pin}", actual=str(hits),
                expected="唯一的物理脚号"))
            return None
        number = part.pin_number(pin)
        if number is None:
            diags.append(Diagnostic(
                "E-IR-PARTS-009", "error", path,
                f"引用了位号 {ref} 不存在的引脚 '{pin}'"
                f"（该器件引脚: {[p.number for p in part.pins]}）",
                stage="connectivity", object_id=f"{ref}.{pin}",
                evidence=part.name))
            return None
        return number

    # ---- 1. 收齐声明（不改写任何东西，也不做"后者覆盖"）----
    for i, net in enumerate(ir.nets):
        for j, pr in enumerate(net.pins):
            path = f"nets[{i}].pins[{j}].pin"
            if pr.ref not in cmap:
                diags.append(Diagnostic(
                    "E-IR-NETS-004", "error", f"nets[{i}].pins[{j}].ref",
                    f"网络 '{net.name}' 引用了不存在的位号 '{pr.ref}'",
                    stage="connectivity", object_id=pr.ref))
                continue
            number = locate(pr.ref, pr.pin, path)
            if number is not None:
                claims.append(Claim(pr.ref, number, net.name, path))

    # ---- 2. 网络内重复 ----
    seen_in_net: dict[tuple[str, str, str], list[str]] = {}
    for c in claims:
        seen_in_net.setdefault((c.net, c.ref, c.number), []).append(c.path)
    for (net, ref, number), paths in seen_in_net.items():
        if len(paths) > 1:
            diags.append(Diagnostic(
                "E-IR-NETS-009", "error", paths[1],
                f"引脚 {ref}.{number} 在网络 '{net}' 里重复出现 {len(paths)} 次"
                f"（同一只脚写两遍不会更可靠，只会掩盖真实意图）",
                stage="connectivity", object_id=f"{ref}.{number}", actual=str(len(paths)),
                expected="1"))

    # ---- 3. 跨网络冲突：一只脚属于多个网络 ----
    by_pin: dict[tuple[str, str], list[Claim]] = {}
    for c in claims:
        by_pin.setdefault((c.ref, c.number), []).append(c)
    conflicted: set[tuple[str, str]] = set()
    for (ref, number), group in by_pin.items():
        nets = sorted({c.net for c in group})
        if len(nets) > 1:
            conflicted.add((ref, number))
            paths = ", ".join(sorted({c.path for c in group}))
            diags.append(Diagnostic(
                "E-IR-PARTS-010", "error", group[0].path,
                f"引脚 {ref}.{number} 同时被网络 {nets} 引用——"
                f"物理上只能属于一个网络，必须删掉多余的归属"
                f"（不采用「后写的赢」）",
                stage="connectivity", object_id=f"{ref}.{number}",
                actual=str(nets), expected="恰好 1 个网络", evidence=paths))

    # ---- 4. 显式不连接声明 ----
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        for pin in comp.nc:
            path = f"components[{i}].nc"
            if part is None:
                continue                     # 器件不在目录里：E-IR-PARTS-003 已报
            number = locate(comp.ref, pin, path)
            if number is None:
                continue
            pin_obj = part.pin(number)
            if pin_obj is not None and not pin_obj.nc_condition:
                diags.append(Diagnostic(
                    "E-IR-PARTS-012", "error", path,
                    f"{comp.ref}.{number}({pin_obj.name}) 被声明为不连接，但"
                    f"该器件不允许这个脚悬空——悬空条件必须写在器件资料里"
                    f"（partsdb 的 nc_condition），不能由 IR 单方面决定",
                    stage="connectivity", object_id=f"{comp.ref}.{number}",
                    expected="该脚的 nc_condition 非空", evidence=part.name))
                continue
            if (comp.ref, number) in by_pin and (comp.ref, number) not in conflicted:
                nets = sorted({c.net for c in by_pin[(comp.ref, number)]})
                diags.append(Diagnostic(
                    "E-IR-PARTS-013", "error", path,
                    f"{comp.ref}.{number}({pin_obj.name if pin_obj else pin}) "
                    f"既被网络 {nets} 连接又被声明不连接——二者矛盾",
                    stage="connectivity", object_id=f"{comp.ref}.{number}",
                    actual=f"连接 {nets} + nc", expected="二者只能有一个"))
                continue
            nc.add((comp.ref, number))

    # ---- 5. 必接引脚 ----
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None:
            continue
        for pin in part.pins:
            if not pin.required:
                continue
            key = (comp.ref, pin.number)
            if key in nc or (key in by_pin and key not in conflicted):
                continue
            if key in conflicted:
                continue                     # 冲突已经报过，不再重复
            diags.append(Diagnostic(
                "E-IR-PARTS-011", "error", f"components[{i}]",
                f"必接引脚 {comp.ref}.{pin.number}({pin.name}) 没有接入任何网络"
                f"——器件资料（{part.name}）把它标为必需；"
                f"若确实要悬空，必须由 nc_condition 允许并在 components[].nc 声明",
                stage="connectivity", object_id=f"{comp.ref}.{pin.number}",
                expected="接入网络或显式 nc", evidence=part.name))

    # ---- 6. 悬空器件（警告，不拦路）----
    touched = {ref for (ref, _) in by_pin} | {ref for (ref, _) in nc}
    orphans = [c.ref for c in ir.components if c.ref not in touched]
    if orphans:
        diags.append(Diagnostic(
            "W-IR-NETS-002", "warning", "nets",
            f"这些器件没接进任何网络（悬空）: {orphans}",
            stage="connectivity", object_id=",".join(orphans)))

    # ---- 7. 建立索引（只放行无冲突的引脚）----
    pin_net: dict[tuple[str, str], str] = {}
    for (ref, number), group in by_pin.items():
        if (ref, number) in conflicted:
            continue
        pin_net[(ref, number)] = group[0].net
    net_pins: dict[str, tuple[str, ...]] = {}
    for (ref, number), net in pin_net.items():
        net_pins.setdefault(net, ())
        net_pins[net] = net_pins[net] + (f"{ref}.{number}",)

    conn = Connectivity(pin_net=pin_net, net_pins=net_pins,
                        nc=frozenset(nc), claims=tuple(claims),
                        diagnostics=tuple(diags))
    return conn, diags
