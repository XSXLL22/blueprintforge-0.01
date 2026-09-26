"""器件事实核对：有效封装、value/nominal、额定值 vs 设计需求、拓扑支持。

与 `checks/` 其它模块同一约定：结论既喂给校验器（报诊断），也喂给生成器
（`preflight` 决定敢不敢画）。理由与连接解析一样——「校验说不行、生成照做」
不能靠两条路径各自记得判同一件事。

**不猜**：解析失败、目录缺该字段、工况无法确定时，一律记入 `unverified`
（由 BuildReport 呈现），绝不退化成"通过"。T00 探针里
`component_ratings_ignored` / `ignored_footprint_override` 的观察值正是
`{"ok": true, "codes": []}`——静默通过，比报错危险得多。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.partsdb import PARTS, Part
from parts.values import (MATCH_CLOSE, MATCH_DIFFER, format_magnitude,
                          magnitudes_match, parse_magnitude)

from checks.connectivity import Connectivity

#: 目录额定值键 → 量纲。只核对能对上量纲的键，对不上的不算"通过"。
_RATING_DIMENSIONS = {
    "voltage_v": "voltage",
    "current_a": "current",
    "power_w": "power",
    "frequency_hz": "frequency",
}

#: 拓扑必需的外围器件类别（`E-IR-PARTS-007`）。
#: 同步 buck 的低边开关在芯片内部，异步 buck 必须外接续流二极管。
TOPOLOGY_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "async_buck": (("inductor", "电感"), ("diode", "续流二极管")),
    "sync_buck": (("inductor", "电感"),),
    "ldo": (),
}

#: 余量提醒阈值：用到额定值的这个比例以上就提醒复核。
MARGIN_RATIO = 0.9


@dataclass(frozen=True)
class PartFacts:
    """核对结论。`footprints` 是**有效封装**（生成器照这个画）。"""

    footprints: dict[str, str | None] = field(default_factory=dict)
    footprint_sources: dict[str, str] = field(default_factory=dict)
    #: 未核对项（缺工况/缺目录字段/无法解析）——不是"通过"
    unverified: tuple[str, ...] = ()

    def footprint_of(self, ref: str) -> str | None:
        return self.footprints.get(ref)


def check(ir: PowerIR, parts: Mapping[str, Part] | None = None,
          conn: Connectivity | None = None) -> tuple[PartFacts, list[Diagnostic]]:
    """全部器件事实核对。返回 (结论, 诊断)。"""
    parts = PARTS if parts is None else parts
    diags: list[Diagnostic] = []
    unverified: list[str] = []

    footprints = _footprints(ir, parts, diags)
    _values(ir, parts, diags)
    _topology(ir, parts, diags, unverified)
    _demands(ir, parts, conn, diags, unverified)

    facts = PartFacts(
        footprints={ref: fp for ref, (fp, _) in footprints.items()},
        footprint_sources={ref: src for ref, (_, src) in footprints.items()},
        unverified=tuple(unverified))
    return facts, diags


# ---- 有效封装 ---------------------------------------------------------------

def _footprints(ir: PowerIR, parts: Mapping[str, Part],
                diags: list[Diagnostic]) -> dict[str, tuple[str | None, str]]:
    """IR 覆盖 → 有效封装。覆盖不在允许集合内就拒绝，不用目录默认值顶替。

    拒绝而不是"忽略覆盖、悄悄用默认值"：后者在 T00 的
    `ignored_footprint_override` 里被复现为 `override_present: false`——
    图纸和网表用的是另一个封装，而 IR 的作者以为他的选择生效了。
    """
    out: dict[str, tuple[str | None, str]] = {}
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None:
            continue                     # 器件不在目录里：E-IR-PARTS-003 已报
        allowed = part.footprints()
        if comp.footprint is None:
            out[comp.ref] = (part.footprint, "catalog" if part.footprint else "none")
            continue
        if comp.footprint in allowed:
            out[comp.ref] = (comp.footprint, "override")
            continue
        out[comp.ref] = (part.footprint, "catalog")
        diags.append(Diagnostic(
            "E-IR-PARTS-014", "error", f"components[{i}].footprint",
            f"位号 {comp.ref}（{part.name}）请求的封装 '{comp.footprint}' "
            f"不在该料的允许集合内——按这个封装画出来的板子与器件实际引脚不符。"
            f"允许：{list(allowed) or '（目录未给允许集合，先补目录）'}",
            stage="parts", object_id=comp.ref,
            actual=comp.footprint, expected=list(allowed),
            evidence=part.mpn or part.name))
    return out


# ---- value 与所选料是否一致 --------------------------------------------------

def _values(ir: PowerIR, parts: Mapping[str, Part],
            diags: list[Diagnostic]) -> None:
    """`components[].value` 必须与目录里该料的标称参数一致。

    `rating`（"3A/40V" 这类注释）**不参与**判定，也不覆盖目录额定值；
    它只能与目录一致，不一致就是矛盾（同一份 IR 里两个说法）。
    """
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None:
            continue
        base = f"components[{i}]"
        if comp.value is not None and part.nominal is not None:
            got = parse_magnitude(comp.value, part.nominal.kind)
            if got is None:
                diags.append(Diagnostic(
                    "E-IR-PARTS-015", "error", f"{base}.value",
                    f"位号 {comp.ref}（{part.name}）的 value '{comp.value}' "
                    f"无法解析成{part.nominal.kind}量——无法核对，不能当一致",
                    stage="parts", object_id=comp.ref, actual=comp.value))
            else:
                verdict = magnitudes_match(got.value, part.nominal.value)
                if verdict == MATCH_DIFFER:
                    diags.append(Diagnostic(
                        "E-IR-PARTS-015", "error", f"{base}.value",
                        f"位号 {comp.ref} 的 value '{comp.value}' 与所选料 "
                        f"{part.name} 的标称值 "
                        f"{format_magnitude(part.nominal.kind, part.nominal.value)} "
                        f"不符",
                        stage="parts", object_id=comp.ref,
                        actual=comp.value,
                        expected=format_magnitude(part.nominal.kind,
                                                  part.nominal.value)))
                elif verdict == MATCH_CLOSE:
                    diags.append(Diagnostic(
                        "W-IR-PARTS-002", "warning", f"{base}.value",
                        f"位号 {comp.ref} 的 value '{comp.value}' 与所选料 "
                        f"{part.name} 的标称值 "
                        f"{format_magnitude(part.nominal.kind, part.nominal.value)} "
                        f"接近但不相同——确认是同一个料",
                        stage="parts", object_id=comp.ref, actual=comp.value))
        if comp.rating is not None:
            _rating_note(ir, comp, part, i, diags)


def _rating_note(ir: PowerIR, comp, part: Part, index: int,
                 diags: list[Diagnostic]) -> None:
    """注释里的额定值只能与目录**一致**，不能覆盖它。

    例：目录写 voltage_v=25 的电容，IR 注释写 "50V" —— 若让注释生效，
    一个 25V 的料就会被当成 50V 用。这里判为矛盾；判定实际用量时用的
    始终是目录值（见 `_demands`）。
    """
    for key, value in part.ratings:
        dim = _RATING_DIMENSIONS.get(key)
        if dim is None:
            continue
        claimed = parse_magnitude(comp.rating, dim)
        if claimed is None:
            continue                     # 注释没提这个量纲，不算矛盾
        if magnitudes_match(claimed.value, value) == MATCH_DIFFER:
            diags.append(Diagnostic(
                "E-IR-PARTS-015", "error", f"components[{index}].rating",
                f"位号 {comp.ref} 的注释额定值 '{comp.rating}' 与所选料 "
                f"{part.name} 的目录额定值 {key}="
                f"{format_magnitude(dim, value)} 矛盾——注释不能覆盖目录，"
                f"要么换料、要么改注释",
                stage="parts", object_id=comp.ref,
                actual=comp.rating, expected=format_magnitude(dim, value)))


# ---- 拓扑支持与必需器件 ------------------------------------------------------

def _topology(ir: PowerIR, parts: Mapping[str, Part],
              diags: list[Diagnostic], unverified: list[str]) -> None:
    """所选器件是否支持该拓扑，以及拓扑必需的器件是否齐备。"""
    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None:
            continue
        supported = part.supports_topology(ir.topology)
        if supported is False:
            diags.append(Diagnostic(
                "E-IR-ELECT-005", "error", f"components[{i}].part",
                f"位号 {comp.ref} 选的 {part.name} 不支持拓扑 "
                f"'{ir.topology}'（它支持 {list(part.supported_topologies)}）"
                f"——器件能力与设计决定矛盾，不是引脚接对就能解决的",
                stage="parts", object_id=comp.ref,
                actual=ir.topology, expected=list(part.supported_topologies)))
        elif supported is None and comp.role == "regulator":
            # 目录没说这只能不能干这个活 = 没核对过。只有稳压器需要记：
            # 电容电阻支不支持某种拓扑这句话本身没有意义，记了只是噪声。
            unverified.append(
                f"{comp.ref}: 目录未声明 {part.name} 支持哪些拓扑，"
                f"未核对它能不能做 '{ir.topology}'")

    have = {c.role for c in ir.components}
    for role, label in TOPOLOGY_REQUIRED.get(ir.topology, ()):
        if role not in have:
            diags.append(Diagnostic(
                "E-IR-PARTS-007", "error", "components",
                f"拓扑 '{ir.topology}' 必须有{label}（role={role}）——"
                f"当前器件表里没有",
                stage="parts", object_id=ir.topology, expected=role))


# ---- 额定值 vs 设计需求 ------------------------------------------------------

def _rails(ir: PowerIR) -> tuple[dict[str, float], list[str]]:
    """网名 → 该网的直流电压（只知道端口声明的输入/输出轨）。"""
    rails = {"GND": 0.0}
    unknown: list[str] = []
    for port in ir.ports:
        if port.direction == "input":
            rails[port.net] = ir.electrical.vin.max
        elif port.direction == "output":
            rails[port.net] = ir.electrical.vout.max
    if not any(p.direction in ("input", "output") for p in ir.ports):
        unknown.append("没有输入/输出端口声明，各网电压未知")
    return rails, unknown


def _exposure(ref: str, part: Part, conn: Connectivity | None,
              rails: dict[str, float]) -> float | None:
    """两脚器件承受的直流电压；任一端电压未知则返回 None（不猜）。"""
    if conn is None or len(part.pins) != 2:
        return None
    a = conn.net_of(ref, part.pins[0].number)
    b = conn.net_of(ref, part.pins[1].number)
    if a is None or b is None or a not in rails or b not in rails:
        return None
    return abs(rails[a] - rails[b])


def _demands(ir: PowerIR, parts: Mapping[str, Part], conn: Connectivity | None,
             diags: list[Diagnostic], unverified: list[str]) -> None:
    """把设计需求（输入/输出范围、电流）与目录额定值对照。"""
    e = ir.electrical
    rails, rail_gap = _rails(ir)
    unverified.extend(rail_gap)

    def over(ref: str, what: str, demand: float, limit: float, source: str,
             dim: str, index: int) -> None:
        diags.append(Diagnostic(
            "E-IR-ELECT-004", "error", f"components[{index}].part",
            f"位号 {ref}（{what}）的需求 {format_magnitude(dim, demand)} 超出"
            f"{source} {format_magnitude(dim, limit)}",
            stage="parts", object_id=ref,
            actual=format_magnitude(dim, demand),
            expected=format_magnitude(dim, limit)))

    def margin(ref: str, what: str, demand: float, limit: float, source: str,
               dim: str, index: int) -> None:
        if limit > 0 and demand / limit >= MARGIN_RATIO:
            diags.append(Diagnostic(
                "W-IR-ELECT-002", "warning", f"components[{index}].part",
                f"位号 {ref}（{what}）用到{source} "
                f"{demand / limit * 100:.0f}%（{format_magnitude(dim, demand)} / "
                f"{format_magnitude(dim, limit)}）——建议复核裕量",
                stage="parts", object_id=ref))

    for i, comp in enumerate(ir.components):
        part = parts.get(comp.part)
        if part is None:
            continue
        ref = comp.ref
        if comp.role == "regulator":
            # 输入范围要落在推荐工作范围内；绝对最大值另判，两者不可混用
            vin_min = part.limit("vin_min")
            vin_max = part.limit("vin_max")
            if vin_min is None and vin_max is None:
                unverified.append(
                    f"{ref}: 目录未给推荐输入范围，未核对输入 "
                    f"{format_magnitude('voltage', e.vin.min)}–"
                    f"{format_magnitude('voltage', e.vin.max)}")
            if vin_min is not None and e.vin.min < vin_min:
                over(ref, part.name, e.vin.min, vin_min, "推荐工作范围下限",
                     "voltage", i)
            if vin_max is not None and e.vin.max > vin_max:
                over(ref, part.name, e.vin.max, vin_max, "推荐工作范围上限",
                     "voltage", i)
            abs_vin = part.absolute_max("vin")
            if abs_vin is not None and e.vin.max > abs_vin:
                over(ref, part.name, e.vin.max, abs_vin, "绝对最大值", "voltage", i)
            vout_min, vout_max = part.limit("vout_min"), part.limit("vout_max")
            if vout_min is not None and e.vout.min < vout_min:
                over(ref, part.name, e.vout.min, vout_min, "输出下限",
                     "voltage", i)
            if vout_max is not None and e.vout.max > vout_max:
                over(ref, part.name, e.vout.max, vout_max, "输出上限",
                     "voltage", i)
            iout_max = part.limit("iout_max")
            if iout_max is None:
                unverified.append(
                    f"{ref}: 目录未给额定输出电流，未核对 "
                    f"{format_magnitude('current', e.iout_max)}")
            else:
                if e.iout_max > iout_max:
                    over(ref, part.name, e.iout_max, iout_max,
                         "推荐工作范围", "current", i)
                else:
                    margin(ref, part.name, e.iout_max, iout_max,
                           "推荐工作范围", "current", i)

        elif part.category == "inductor":
            cur = part.rating("current_a")
            if cur is None:
                unverified.append(f"{ref}: 目录未给额定电流，未核对是否够 {e.iout_max}A")
            elif e.iout_max > cur:
                over(ref, part.name, e.iout_max, cur, "额定电流", "current", i)
            elif e.iout_max / cur >= MARGIN_RATIO:
                margin(ref, part.name, e.iout_max, cur, "额定电流", "current", i)

        elif part.category == "diode":
            cur = part.rating("current_a")
            if cur is None:
                unverified.append(f"{ref}: 目录未给额定电流，未核对")
            elif e.iout_max > cur:
                over(ref, part.name, e.iout_max, cur, "额定电流", "current", i)
            vmax = part.rating("voltage_v")
            if vmax is None:
                unverified.append(f"{ref}: 目录未给耐压，未核对反向电压")
            else:
                # 异步 buck 的续流二极管在开关管关断时承受输入电压
                if e.vin.max > vmax:
                    over(ref, part.name, e.vin.max, vmax, "反向耐压", "voltage", i)

        elif part.category == "capacitor":
            vmax = part.rating("voltage_v")
            if vmax is None:
                unverified.append(f"{ref}: 目录未给耐压，未核对")
                continue
            exposure = _exposure(ref, part, conn, rails)
            if exposure is None:
                unverified.append(
                    f"{ref}: 两端电压无法确定（网电压未知），未核对耐压"
                    f" {format_magnitude('voltage', vmax)}")
            elif exposure > vmax:
                over(ref, part.name, exposure, vmax, "额定电压", "voltage", i)
