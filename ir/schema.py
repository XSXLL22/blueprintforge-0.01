"""IR v0.1 —— 与 EDA 无关的电路中间表示。

IR 是 LLM 与后端之间**唯一**的交接面：

- LLM 只产出/修改 IR（结构化 JSON），永不直接读写 KiCad 工程文件；
- 后端（生成器 / 校验器 / 仿真器）只消费 IR，不依赖 LLM 的文本习惯。

本模块只定义数据结构与序列化；规则校验在 ir/validate.py。
版本规则：字段只增不删（向后兼容），校验规则可以收紧，schema 结构不轻易变。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = "0.1"

#: v0.1 只支持电源模块。后续按「模块库」扩展（MCU / 接口 / 执行器）。
MODULE_TYPES = ("power.buck", "power.ldo")

#: 器件在电路中的角色（用于校验与生成器的分工）。
ROLES = ("regulator", "inductor", "capacitor", "resistor", "diode",
         "connector", "passive")

#: 每个 module_type 允许的拓扑。
TOPOLOGIES: dict[str, tuple[str, ...]] = {
    "power.buck": ("async_buck", "sync_buck"),
    "power.ldo": ("ldo",),
}


@dataclass
class Range:
    """一个电气量的 min/typ/max，单位见字段所在上下文。"""
    min: float
    typ: float
    max: float


@dataclass
class Electrical:
    """模块的电气边界条件。"""
    vin: Range          # 输入电压（V）
    vout: Range         # 输出电压（V）
    iout_max: float     # 最大输出电流（A）


@dataclass
class Component:
    """一个元器件的选型与参数。"""
    ref: str            # 位号，如 "U1"
    part: str           # 器件库型号，必须在 parts/partsdb.py 中存在
    role: str           # 见 ROLES
    value: str | None = None      # 阻容感参数，如 "49.9k" "10uF" "4.7uH"
    footprint: str | None = None  # KiCad 封装名；None 时由生成器按器件库映射
    rating: str | None = None     # 额定值标注，如 "25V" "3A"


@dataclass(frozen=True)
class PinRef:
    """网络里对某个器件引脚的引用。"""
    ref: str
    pin: str


@dataclass
class Net:
    """一条网络及其引脚列表。"""
    name: str
    pins: tuple[PinRef, ...]
    netclass: str | None = None   # "power" | "signal"；None 由后端推断


@dataclass
class PowerIR:
    """一个电源模块的完整 IR。"""
    schema_version: str
    project: str
    module_type: str
    topology: str
    electrical: Electrical
    components: tuple[Component, ...]
    nets: tuple[Net, ...]
    constraints: dict = field(default_factory=dict)   # 如 {"efficiency_target": ">85%"}
    design_rationale: str = ""    # 必填（缺了是警告）：为什么选这个拓扑/这些器件
    assumptions: tuple[str, ...] = ()   # 设计假设（负载条件/温度/频率）
    risks: tuple[str, ...] = ()         # 风险标记（哪些必须实测/复核）
    raw: dict = field(default_factory=dict)

    @property
    def component_map(self) -> dict[str, Component]:
        return {c.ref: c for c in self.components}


def _range(d: dict) -> Range:
    return Range(min=float(d["min"]), typ=float(d["typ"]), max=float(d["max"]))


def from_dict(raw: dict) -> PowerIR:
    """把 JSON 字典转成 PowerIR。

    只做类型转换，不校验语义（校验见 validate.py）。字段缺失/类型错误
    会抛 KeyError/TypeError，由 load() 包装成带路径的错误。
    """
    elec = raw["electrical"]
    components = tuple(
        Component(
            ref=c["ref"], part=c["part"], role=c.get("role", "passive"),
            value=c.get("value"), footprint=c.get("footprint"),
            rating=c.get("rating"),
        )
        for c in raw.get("components", [])
    )
    nets = tuple(
        Net(
            name=n["name"],
            pins=tuple(PinRef(p["ref"], p["pin"]) for p in n.get("pins", [])),
            netclass=n.get("netclass"),
        )
        for n in raw.get("nets", [])
    )
    return PowerIR(
        schema_version=raw.get("schema_version", SCHEMA_VERSION),
        project=raw["project"],
        module_type=raw["module_type"],
        topology=raw["topology"],
        electrical=Electrical(
            vin=_range(elec["vin"]),
            vout=_range(elec["vout"]),
            iout_max=float(elec["iout_max"]),
        ),
        components=components,
        nets=nets,
        constraints=dict(raw.get("constraints", {})),
        design_rationale=str(raw.get("design_rationale", "")),
        assumptions=tuple(str(a) for a in raw.get("assumptions", [])),
        risks=tuple(str(r) for r in raw.get("risks", [])),
        raw=raw,
    )


def to_dict(ir: PowerIR) -> dict:
    """结构化输出，供落盘/回灌。只保留有效字段，与 from_dict 互逆。"""
    def rng(r: Range) -> dict:
        return {"min": r.min, "typ": r.typ, "max": r.max}

    def comp(c: Component) -> dict:
        d = {"ref": c.ref, "part": c.part, "role": c.role}
        if c.value is not None:
            d["value"] = c.value
        if c.footprint is not None:
            d["footprint"] = c.footprint
        if c.rating is not None:
            d["rating"] = c.rating
        return d

    return {
        "schema_version": ir.schema_version,
        "project": ir.project,
        "module_type": ir.module_type,
        "topology": ir.topology,
        "electrical": {
            "vin": rng(ir.electrical.vin),
            "vout": rng(ir.electrical.vout),
            "iout_max": ir.electrical.iout_max,
        },
        "components": [comp(c) for c in ir.components],
        "nets": [
            {k: v for k, v in {
                "name": n.name, "netclass": n.netclass,
                "pins": [{"ref": p.ref, "pin": p.pin} for p in n.pins],
            }.items() if v is not None}
            for n in ir.nets
        ],
        "constraints": ir.constraints,
        "design_rationale": ir.design_rationale,
        "assumptions": list(ir.assumptions),
        "risks": list(ir.risks),
    }


def load(path: str | Path) -> PowerIR:
    """从 JSON 文件加载 IR。文件/JSON/字段错误统一抛 ValueError。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"IR 文件不存在: {p}") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"IR JSON 解析失败: {e}") from None
    try:
        return from_dict(raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"IR 字段缺失或类型错误: {e}") from None
