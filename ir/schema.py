"""IR v0.2 —— 与 EDA 无关的电路中间表示。

IR 是 LLM 与后端之间**唯一**的交接面：

- LLM 只产出/修改 IR（结构化 JSON），永不直接读写 KiCad 工程文件；
- 后端（生成器 / 校验器 / 仿真器）只消费 IR，不依赖 LLM 的文本习惯。

v0.2 相对 v0.1 的增量（都能从 v0.1 迁移，见 ir/migrate.py）：

| 变化 | v0.1 | v0.2 |
|---|---|---|
| 设计目标 | `constraints` 自由文本 `">85%"` | `targets` 数值 + 单位（`efficiency_min: 0.85`） |
| 外部端口 | 无 | `ports`：输入/输出/地及**驱动来源** |
| 显式不连接 | 无（生成器自动补 NC） | `components[].nc`：只有显式声明才允许悬空 |

本模块只定义数据结构与序列化；**严格解析在 ir/decode.py**，
语义校验在 ir/validate.py。三者的分工：

    decode.py  结构/类型/语法 → 不通过就绝不进入语义检查
    schema.py  数据结构、序列化、迁移后的统一内存模型
    validate.py 语义：范围、拓扑、连接、器件

`from_dict` 保留为宽松入口（测试与工具用，容忍语义错误），
`load` 走严格解码。两者共用同一套数据类，不产生第二套语义。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

#: 当前版本。
SCHEMA_VERSION = "0.2"

#: 声明支持的版本（0.1 通过迁移进入 0.2，不能自动继承验证状态）。
SUPPORTED_VERSIONS = ("0.1", "0.2")

#: v0.2 只支持电源模块。后续按「模块库」扩展（MCU / 接口 / 执行器）。
MODULE_TYPES = ("power.buck", "power.ldo")

#: 器件在电路中的角色（用于校验与生成器的分工）。
ROLES = ("regulator", "inductor", "capacitor", "resistor", "diode",
         "connector", "passive")

#: 每个 module_type 允许的拓扑。
TOPOLOGIES: dict[str, tuple[str, ...]] = {
    "power.buck": ("async_buck", "sync_buck"),
    "power.ldo": ("ldo",),
}

#: 目标键 → (比较方向, 量纲, 一句话含义)。
#: 量纲决定单位与解析方式（parts/values.py 的 DIMENSIONS）。
#: 未登记的键**不允许**出现——拼错的 `efficency_min` 不能变成被忽略的约束。
#: 键名本身带单位（`_v` / `_cny` / `_a` / `_c`），比值类键名**不带** `pct`，
#: 值一律是分数（0.02 = ±2%）——避免"键说百分比、值写分数"的 100 倍错误。
TARGETS: dict[str, tuple[str, str, str]] = {
    "ripple_max_v": ("max", "voltage", "输出纹波峰值上限（V）"),
    "efficiency_min": ("min", "ratio", "满载效率下限（分数，0.85 = 85%）"),
    "bom_cost_max_cny": ("max", "cost", "BOM 成本上限（人民币元）"),
    "vout_accuracy": ("max", "ratio", "输出电压精度（分数，0.02 = ±2%）"),
    "ambient_max_c": ("max", "temperature", "最高环境温度（°C）"),
    "iout_max_a": ("max", "current", "最大输出电流（A）"),
    "transient_dev": ("max", "ratio", "负载跳变允许偏差（分数，0.05 = 5%）"),
}

#: 目标值的「常理范围」。超出只报警告：它的作用是抓单位错误
#: （mV↔V 差 1000 倍、%↔分数差 100 倍），不是替设计者定指标。
TARGET_PLAUSIBLE: dict[str, tuple[float, float]] = {
    "ripple_max_v": (1e-4, 5.0),
    "efficiency_min": (0.3, 1.0),
    "bom_cost_max_cny": (0.1, 1e6),
    "vout_accuracy": (1e-4, 0.5),
    "ambient_max_c": (-40.0, 200.0),
    "iout_max_a": (1e-6, 1e4),
    "transient_dev": (1e-4, 1.0),
}

#: 端口方向。
PORT_DIRECTIONS = ("input", "output", "bidirectional")

#: 顶层允许的键。`x_ext` 是**唯一**的扩展区：里面的东西不参与校验，
#: 也不会被当成约束——避免拼写错误悄悄变成"没生效的约束"。
TOP_LEVEL_KEYS = (
    "schema_version", "project", "module_type", "topology", "electrical",
    "components", "nets", "targets", "ports", "design_rationale",
    "assumptions", "risks", "x_ext",
)

#: electrical 允许的键。
ELECTRICAL_KEYS = ("vin", "vout", "iout_max", "x_ext")

#: components[] 元素允许的键。
COMPONENT_KEYS = ("ref", "part", "role", "value", "footprint", "rating",
                  "nc", "x_ext")

#: nets[] 元素允许的键。
NET_KEYS = ("name", "pins", "netclass", "x_ext")

#: pins[] 元素允许的键。
PIN_KEYS = ("ref", "pin")

#: ports[] 元素允许的键。
PORT_KEYS = ("name", "direction", "net", "drives", "note", "x_ext")

#: netclass 允许的取值。
NETCLASSES = ("power", "signal")


@dataclass(frozen=True)
class Range:
    """一个电气量的 min/typ/max，单位见字段所在上下文。"""
    min: float
    typ: float
    max: float


@dataclass(frozen=True)
class Electrical:
    """模块的电气边界条件。"""
    vin: Range          # 输入电压（V），推荐工作范围
    vout: Range         # 输出电压（V）
    iout_max: float     # 最大输出电流（A）


@dataclass(frozen=True)
class Target:
    """一个设计目标：数值 + 单位（量纲），可选验证条件。

    `conditions` 为空表示"没有约定测量/仿真条件"——这类目标**不能**
    被盖通过章（§4.1：未定义条件时不能给对应指标盖通过章）。
    """
    key: str            # 见 TARGETS
    kind: str           # "min" | "max"
    value: float        # SI/约定单位下的数值
    dimension: str      # 量纲
    source: str = ""    # 迁移来源（如 '">85%"'），便于追溯
    conditions: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Port:
    """外部端口：某个网络由模块外部提供或引出的声明。

    用途：限制 PWR_FLAG 的使用——只有能证明来源（power_out 引脚 / 外部端口 /
    串联无源元件构成的路径）的电源网络才该被标记为已驱动。
    """
    name: str           # 端口名，如 "VIN_IN"
    direction: str      # 见 PORT_DIRECTIONS
    net: str            # 对应的网络名
    drives: bool = False    # 是否由外部**驱动**该网络（供电来源）
    note: str = ""


@dataclass(frozen=True)
class Component:
    """一个元器件的选型与参数。"""
    ref: str            # 位号，如 "U1"
    part: str           # 器件库型号，必须在器件目录中存在
    role: str           # 见 ROLES
    value: str | None = None      # 阻容感参数，如 "49.9k" "10uF" "4.7uH"
    footprint: str | None = None  # KiCad 封装名；None 时由目录映射
    rating: str | None = None     # 额定值**标注**（注释用，不覆盖目录额定值）
    #: 显式声明不连接的引脚。只有这里列出的引脚才允许悬空。
    nc: tuple[str, ...] = ()


@dataclass(frozen=True)
class PinRef:
    """网络里对某个器件引脚的引用（可以是功能名，见 ResolvedIR）。"""
    ref: str
    pin: str


@dataclass(frozen=True)
class Net:
    """一条网络及其引脚列表。"""
    name: str
    pins: tuple[PinRef, ...]
    netclass: str | None = None   # "power" | "signal"；None 由后端推断


@dataclass(frozen=True)
class PowerIR:
    """一个电源模块的完整 IR（v0.2）。

    引脚引用允许写成功能名（`"VIN"`）或物理号（`"7"`）；把名称消歧成物理
    编号、绑定有效封装/额定值之后的产物是 ResolvedIR（见 ir/resolved.py）。
    """
    schema_version: str
    project: str
    module_type: str
    topology: str
    electrical: Electrical
    components: tuple[Component, ...]
    nets: tuple[Net, ...]
    targets: tuple[Target, ...] = ()
    ports: tuple[Port, ...] = ()
    design_rationale: str = ""
    assumptions: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)   # 解码/迁移后的 v0.2 原始字典

    @property
    def component_map(self) -> dict[str, Component]:
        return {c.ref: c for c in self.components}

    @property
    def net_map(self) -> dict[str, Net]:
        return {n.name: n for n in self.nets}

    def target(self, key: str) -> Target | None:
        for t in self.targets:
            if t.key == key:
                return t
        return None


# ---- 序列化 -----------------------------------------------------------------

def _number(value: object, path: str) -> float:
    """宽松入口的数值转换。

    「宽松」指的是**不做交叉检查**（器件是否存在、值是否合理），
    不是指类型可以含糊：

    - `bool` 必须拒绝——它是 `int` 的子类，`float(True)` 会静默变成 1.0，
      那正是「布尔冒充数值」这个缺陷本身；
    - 字符串必须拒绝——`"2.0"` 看着无害，但它是"字符串冒充数值"入口，
      一旦允许，`"2A"`、`"2.0 "`、`"２.０"` 的边界就只能靠猜；
    - 非有限浮点（NaN/Infinity）**放行**给 validate：语义层会用
      `E-IR-DECODE-004` 给出带字段路径的诊断，比在这里抛异常更有用。
    """
    if isinstance(value, bool):
        raise TypeError(
            f"{path} 是布尔值，不能冒充数值（true/false 会被当成 1/0）——"
            f"需要结构化诊断请用 ir.decode.decode_ir")
    if not isinstance(value, (int, float)):
        raise TypeError(
            f"{path} 不是数值: {value!r}（宽松入口也不把字符串当数值——"
            f"需要结构化诊断请用 ir.decode.decode_ir）")
    return float(value)


def _range(d: dict, path: str = "electrical") -> Range:
    return Range(min=_number(d["min"], f"{path}.min"),
                 typ=_number(d["typ"], f"{path}.typ"),
                 max=_number(d["max"], f"{path}.max"))


def _target_from_dict(key: str, data) -> Target:
    """内部使用：把 v0.2 的 targets 取值转成 Target。

    接受两种写法：`{"efficiency_min": 0.85}`（只给数值，方向/量纲查表）
    与 `{"efficiency_min": {"value": 0.85, "conditions": {...}}}`。
    """
    if key not in TARGETS:
        raise ValueError(
            f"未登记的设计目标键 {key!r}——拼错的约束不会被静默忽略"
            f"（支持: {', '.join(sorted(TARGETS))}）；"
            f"结构化诊断请用 ir.decode.decode_ir")
    kind, dimension, _doc = TARGETS[key]
    if isinstance(data, dict):
        conditions = data.get("conditions") or {}
        return Target(key=key, kind=kind, value=float(data["value"]),
                      dimension=dimension, source=str(data.get("source", "")),
                      conditions=dict(conditions))
    return Target(key=key, kind=kind, value=float(data), dimension=dimension)


def _component_from_dict(c: dict) -> Component:
    nc = c.get("nc") or ()
    return Component(
        ref=c["ref"], part=c["part"], role=c.get("role", "passive"),
        value=c.get("value"), footprint=c.get("footprint"),
        rating=c.get("rating"),
        nc=tuple(str(x) for x in nc),
    )


def _net_from_dict(n: dict) -> Net:
    return Net(
        name=n["name"],
        pins=tuple(PinRef(p["ref"], p["pin"]) for p in n.get("pins", [])),
        netclass=n.get("netclass"),
    )


def from_dict(raw: dict) -> PowerIR:
    """宽松入口：把字典转成 PowerIR。

    容忍**语义**错误（值不合理、器件不存在等），交给 validate 诊断；
    也容忍 v0.1 的 `constraints`（自动按迁移规则换算，并记录 source）。
    结构错误（缺字段、类型不对）仍会抛 KeyError/TypeError——
    需要结构化诊断的调用方请用 `ir.decode.decode_ir`。
    """
    if not isinstance(raw, dict):
        raise TypeError(f"IR 顶层必须是对象，实际 {type(raw).__name__}")

    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        # 走同一条迁移路径（ir/migrate.py），不另写一套换算。
        # 迁移**报错**就不能继续：看不懂的约束一旦被丢掉，下游会误以为
        # 所有约束都满足了（而"没报错的约束"正是最危险的缺失）。
        from ir.migrate import migrate_ir
        mdiags: list = []
        raw, _notes = migrate_ir(raw, mdiags)
        errors = [d for d in mdiags if d.is_error]
        if errors:
            detail = "\n".join(f"  {d.code} [{d.path}] {d.message}"
                               for d in errors)
            raise ValueError(f"IR 迁移失败（{len(errors)} 个错误）:\n{detail}")
        version = raw.get("schema_version", version)
    targets_raw = raw.get("targets")

    elec = raw["electrical"]
    return PowerIR(
        schema_version=version,
        project=raw["project"],
        module_type=raw["module_type"],
        topology=raw["topology"],
        electrical=Electrical(
            vin=_range(elec["vin"], "electrical.vin"),
            vout=_range(elec["vout"], "electrical.vout"),
            iout_max=_number(elec["iout_max"], "electrical.iout_max"),
        ),
        components=tuple(_component_from_dict(c)
                         for c in raw.get("components", [])),
        nets=tuple(_net_from_dict(n) for n in raw.get("nets", [])),
        targets=tuple(_target_from_dict(k, v)
                      for k, v in (targets_raw or {}).items()),
        ports=tuple(
            Port(name=p["name"], direction=p.get("direction", "input"),
                 net=p["net"], drives=bool(p.get("drives", False)),
                 note=str(p.get("note", "")))
            for p in raw.get("ports", [])
        ),
        design_rationale=str(raw.get("design_rationale", "")),
        assumptions=tuple(str(a) for a in raw.get("assumptions", [])),
        risks=tuple(str(r) for r in raw.get("risks", [])),
        raw=raw,
    )


def _target_to_dict(t: Target):
    if t.conditions or t.source:
        d: dict = {"value": t.value}
        if t.conditions:
            d["conditions"] = dict(t.conditions)
        if t.source:
            d["source"] = t.source
        return d
    return t.value


def to_dict(ir: PowerIR) -> dict:
    """结构化输出，供落盘/回灌。只保留有效字段，与 from_dict 互逆。"""
    def rng(r: Range) -> dict:
        return {"min": r.min, "typ": r.typ, "max": r.max}

    def comp(c: Component) -> dict:
        d: dict = {"ref": c.ref, "part": c.part, "role": c.role}
        if c.value is not None:
            d["value"] = c.value
        if c.footprint is not None:
            d["footprint"] = c.footprint
        if c.rating is not None:
            d["rating"] = c.rating
        if c.nc:
            d["nc"] = list(c.nc)
        return d

    def port(p: Port) -> dict:
        d: dict = {"name": p.name, "direction": p.direction, "net": p.net}
        if p.drives:
            d["drives"] = True
        if p.note:
            d["note"] = p.note
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
        "targets": {t.key: _target_to_dict(t) for t in ir.targets},
        "ports": [port(p) for p in ir.ports],
        "design_rationale": ir.design_rationale,
        "assumptions": list(ir.assumptions),
        "risks": list(ir.risks),
    }


def dump(ir: PowerIR, path: str | Path) -> Path:
    """把 IR 落盘为 v0.2 JSON。"""
    p = Path(path)
    p.write_text(json.dumps(to_dict(ir), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


# ---- 加载 -------------------------------------------------------------------

def load(path: str | Path) -> PowerIR:
    """从 JSON 文件加载 IR（**严格**：结构/类型/语法错误一律拒绝）。

    文件与 JSON 文本错误抛 ValueError；结构错误抛 ValueError 并附上
    结构化诊断（错误码 + 字段路径）。v0.1 输入会自动迁移并记录转换。
    """
    from ir import decode

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"IR 文件不存在: {p}") from None
    except UnicodeDecodeError as e:
        raise ValueError(f"IR 文件不是 UTF-8 文本: {p}（{e}）") from None

    result = decode.decode_text(text, source=str(p))
    if not result.usable:
        detail = "\n".join(f"  {d.code} [{d.path}] {d.message}"
                           for d in result.diagnostics)
        raise ValueError(f"IR 校验失败（{len(result.errors)} 个结构错误）:\n{detail}")
    assert result.draft is not None
    return result.draft


def loads(text: str, *, source: str = "<string>") -> PowerIR:
    """从 JSON 文本加载 IR（严格），供测试与管道使用。"""
    from ir import decode

    result = decode.decode_text(text, source=source)
    if not result.usable:
        detail = "\n".join(f"  {d.code} [{d.path}] {d.message}"
                           for d in result.diagnostics)
        raise ValueError(f"IR 校验失败（{len(result.errors)} 个结构错误）:\n{detail}")
    assert result.draft is not None
    return result.draft


#: 有限性检查的统一入口（避免各处重复 math.isfinite）。
def is_finite(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))
