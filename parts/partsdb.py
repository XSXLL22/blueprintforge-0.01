"""器件库 —— 「市场资材」+「工程积木」的单一事实来源。

每个器件除市场字段（价格/库存/生命周期）外，还携带工程字段：
- lib_id   官方符号引用（None 表示生成器内嵌自绘方框符号，用于官方库没有的 IC）
- footprint KiCad 封装名（"库:名"）
- pins     引脚表 ((编号, 名称, 电气类型), ...)——校验器与生成器共用

v0.1 是 stub：价格为占位值、stock 为占位数、lcsc 为空（接 API 后自动填充）。
**占位数据只用于开发流程，禁止用于真实采购决策。**
接入供应链 API 时只换数据来源，字段不变——LLM 与校验器看到的永远是同一张表。

lifecycle 约定（对齐行业术语）：
- "active"：正常供应
- "nrfnd"：Not Recommended for New Designs，可用但不建议新设计
- "eol"   ：已停产，硬错误
"""
from __future__ import annotations

from dataclasses import dataclass

#: 器件类别（与 IR 的 role 对应，见 ir/validate.py ROLE_CATEGORY）。
CATEGORIES = ("regulator", "inductor", "capacitor", "resistor",
              "diode", "connector")


@dataclass(frozen=True)
class Part:
    name: str           # 器件库型号（IR 里引用的键）
    category: str       # 见 CATEGORIES
    package: str        # 封装描述（给人看）
    spec: str           # 关键参数摘要，给 LLM 选型用的一句话
    lifecycle: str      # "active" | "nrfnd" | "eol"
    lib_id: str | None = None      # KiCad 官方符号 "库:名"；None=内嵌自绘符号
    footprint: str | None = None   # KiCad 封装 "库:名"
    pins: tuple[tuple[str, str, str], ...] = ()  # ((编号, 名称, 电气类型), ...)
    price_cny: float | None = None    # 单价（元），None=未知
    stock: int | None = None          # 库存，None=未知
    lcsc: str | None = None           # 立创编号，接 API 后填充
    alternatives: tuple[str, ...] = ()


#: 器件表。IR 里 components[].part 必须命中这里。
#: 官方符号的引脚编号/名称已对照本机 KiCad 10.0 官方库核实（tests/test_schematic.py
#: 在装有 KiCad 的机器上会再次核对几何）。
PARTS: dict[str, Part] = {
    # ---- 稳压器 ----
    "MP1584EN": Part(
        name="MP1584EN", category="regulator", package="SOIC-8(EP)",
        spec="DC-DC 降压，4.5–28V 输入，0.8–25V 输出，3A，最高 1.5MHz",
        lifecycle="active", lib_id=None,
        footprint="Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm",
        pins=(("1", "BST", "passive"), ("2", "GND", "power_in"),
              ("3", "FB", "input"), ("4", "EN", "input"),
              ("5", "COMP", "passive"), ("6", "VIN", "power_in"),
              ("7", "SW", "power_out"), ("8", "PGND", "power_in"),
              ("EP", "EP", "passive")),
        price_cny=1.2, stock=5000),
    "AMS1117-3.3": Part(
        name="AMS1117-3.3", category="regulator", package="SOT-223",
        spec="LDO，固定 3.3V 输出，最大 1A，压差约 1.1V",
        lifecycle="active", lib_id=None,
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        pins=(("1", "GND", "power_in"), ("2", "VOUT", "power_out"),
              ("3", "VIN", "power_in")),
        price_cny=0.15, stock=80000),

    # ---- 电感 ----
    "IND-POW-10uH-3A": Part(
        name="IND-POW-10uH-3A", category="inductor", package="6.3x6.3",
        spec="功率电感 10µH，饱和电流 ≥3A（具体型号待器件库细化）",
        lifecycle="active", lib_id="Device:L",
        footprint="Inductor_SMD:L_6.3x6.3_H3",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.6, stock=20000),
    "IND-POW-4.7uH-3A": Part(
        name="IND-POW-4.7uH-3A", category="inductor", package="6.3x6.3",
        spec="功率电感 4.7µH，饱和电流 ≥3A",
        lifecycle="active", lib_id="Device:L",
        footprint="Inductor_SMD:L_6.3x6.3_H3",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.55, stock=20000),

    # ---- 电容 ----
    "CAP-0805-10uF-25V-X5R": Part(
        name="CAP-0805-10uF-25V-X5R", category="capacitor", package="0805",
        spec="MLCC 10µF/25V X5R",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0805_2012Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.05, stock=100000),
    "CAP-1206-22uF-25V-X5R": Part(
        name="CAP-1206-22uF-25V-X5R", category="capacitor", package="1206",
        spec="MLCC 22µF/25V X5R",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_1206_3216Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.08, stock=100000),
    "CAP-0402-0.1uF-25V": Part(
        name="CAP-0402-0.1uF-25V", category="capacitor", package="0402",
        spec="MLCC 0.1µF/25V",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.01, stock=500000),
    "CAP-0402-10nF-25V": Part(
        name="CAP-0402-10nF-25V", category="capacitor", package="0402",
        spec="MLCC 10nF/25V",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.01, stock=500000),

    # ---- 电阻 ----
    "RES-0603-49.9k-1%": Part(
        name="RES-0603-49.9k-1%", category="resistor", package="0603",
        spec="厚膜 49.9kΩ ±1%",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.008, stock=200000),
    "RES-0603-16k-1%": Part(
        name="RES-0603-16k-1%", category="resistor", package="0603",
        spec="厚膜 16kΩ ±1%",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.008, stock=200000),
    "RES-0603-3.3k-1%": Part(
        name="RES-0603-3.3k-1%", category="resistor", package="0603",
        spec="厚膜 3.3kΩ ±1%",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.008, stock=200000),

    # ---- 二极管 / 接插件 ----
    "SS34": Part(
        name="SS34", category="diode", package="SMA",
        spec="肖特基二极管 3A/40V（buck 续流用）",
        lifecycle="active", lib_id="Device:D_Schottky",
        footprint="Diode_SMD:D_SMA",
        pins=(("1", "K", "passive"), ("2", "A", "passive")),
        price_cny=0.3, stock=30000),
    "XH2.54-2P": Part(
        name="XH2.54-2P", category="connector", package="XH-2P-2.54mm",
        spec="JST XH 2.54mm 2P 接插件（电源输入/输出端子）",
        lifecycle="active", lib_id="Connector_Generic:Conn_01x02",
        footprint="Connector_JST:JST_XH_B2B-XH-AM_1x02_P2.50mm_Vertical",
        pins=(("1", "1", "passive"), ("2", "2", "passive")),
        price_cny=0.1, stock=50000),
}


def find(name: str) -> Part | None:
    """按型号查器件，查无返回 None。"""
    return PARTS.get(name)


def resolve_pin(part: Part, pin: str) -> str | None:
    """IR 里的引脚引用（编号或名称）→ 统一引脚编号。查无返回 None。"""
    for number, name, _ in part.pins:
        if pin == number or pin == name:
            return number
    return None
