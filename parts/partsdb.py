"""器件库 —— 「市场资材」+「工程积木」的单一事实来源。

每条记录分三层：

1. **事实层**（`sources` / `pins` / `operating` / `ratings` / `nominal`）：
   必须能追到原厂资料或明确的系列通用值，并标注 `review_state`。
   `review_state="sourced"` = 已按原厂资料逐项核对（记了页码）；
   `review_state="generic"` = 泛化料号/系列典型值，**不允许**当作已核对的
   采购事实，也不允许用于制造阶段。
2. **映射层**（`lib_id` / `footprint` / `allowed_footprints`）：
   符号引用与 KiCad 封装。`lib_id=None` 表示用生成器内嵌自绘方框符号。
   `allowed_footprints` 是**允许的封装集合**（含默认封装），IR 的 footprint
   覆盖只有落在这个集合里才生效。
3. **市场层**（`price_cny` / `stock` / `lcsc` / `lifecycle`）：v0.1 是 stub，
   价格为占位值，**禁止用于真实采购决策**。

引脚表 `pins` 是校验器与生成器的唯一事实来源：
- `number` 必须与**所选封装的焊盘编号**一致（KiCad 靠 pad 号 ↔ 符号脚号连接）；
  SOIC8E 的散热焊盘在 footprint 里是 pad `9`（`pad_prop_heatsink`），
  因此符号里也必须有一个脚号 `9` 的引脚，否则 PCB 上散热焊盘会没有网络。
- `kind` 是 KiCad 电气类型（power_in / power_out / input / passive / ...），
  ERC 与「网络是否有驱动」的判断都以此为准。
- `required=True` 表示该脚必须连进网络；允许悬空的脚必须给出
  `nc_condition`（器件资料里的允许条件），并只在该脚被 IR 显式声明 NC 时才算合法。

lifecycle 约定（对齐行业术语）：
- "active"：正常供应
- "nrfnd"：Not Recommended for New Designs，可用但不建议新设计
- "eol"   ：已停产，硬错误
"""
from __future__ import annotations

from dataclasses import dataclass, field

from parts.rules import CapBetween, NotTiedToPin, RToNet, SeriesRCToNet

#: 器件类别（与 IR 的 role 对应，见 checks/catalog.py ROLE_CATEGORY）。
CATEGORIES = ("regulator", "inductor", "capacitor", "resistor",
              "diode", "connector")

#: review_state 取值。
REVIEW_STATES = ("sourced", "generic")


@dataclass(frozen=True)
class Source:
    """一条可追溯的资料出处。页码/条件必须写清楚，不能只给链接。"""
    kind: str        # "datasheet" | "product_page" | "footprint" | "series"
    url: str
    revision: str    # 文档版本/修订日期
    pages: str       # 用到的页码（可以多个）
    retrieved: str   # 复核日期（ISO）
    note: str = ""   # 该页/该资料支持了哪些字段


@dataclass(frozen=True)
class Pin:
    """一个引脚。`number` 必须与所选封装的焊盘编号一致。"""
    number: str
    name: str
    kind: str            # KiCad 电气类型
    required: bool = True
    nc_condition: str = ""   # 非空 = 该脚允许显式悬空，字符串是允许条件


@dataclass(frozen=True)
class Param:
    """一个有单位的标称参数：(量纲, 数值, 单位)。数值用 SI 基本单位。"""
    kind: str        # "resistance" | "capacitance" | "inductance"
    value: float
    unit: str        # "ohm" | "F" | "H"


@dataclass(frozen=True)
class Part:
    name: str           # 器件库型号（IR 里引用的键）
    category: str       # 见 CATEGORIES
    package: str        # 封装描述（给人看）
    spec: str           # 关键参数摘要，给 LLM 选型用的一句话
    lifecycle: str      # "active" | "nrfnd" | "eol"
    lib_id: str | None = None      # KiCad 官方符号 "库:名"；None=内嵌自绘符号
    footprint: str | None = None   # 默认 KiCad 封装 "库:名"
    pins: tuple[Pin, ...] = ()     # 引脚表（按编号顺序）
    price_cny: float | None = None    # 单价（元），None=未知
    stock: int | None = None          # 库存，None=未知
    lcsc: str | None = None           # 立创编号，接 API 后填充
    alternatives: tuple[str, ...] = ()

    #: 制造商名；泛化料号为 None。
    manufacturer: str | None = None
    #: 准确 MPN（可采购型号）；泛化料号为 None。
    mpn: str | None = None
    #: 允许的封装集合（含默认）。IR 的 footprint 覆盖必须命中这里。
    allowed_footprints: tuple[str, ...] = ()
    #: 标称参数（阻容感），用于校验 IR 的 value 是否与所选料一致。
    nominal: Param | None = None
    #: 电气特性/能力：容差、限流、耐压等（键名见下）。
    ratings: tuple[tuple[str, float], ...] = ()
    #: **推荐工作范围**（不是绝对最大值），键名见下。
    operating: tuple[tuple[str, float], ...] = ()
    #: **绝对最大值**。绝不能当作允许的长期工作点。
    abs_max: tuple[tuple[str, float], ...] = ()
    #: 该器件支持的 IR 拓扑（与 ir.schema.TOPOLOGIES 同一套词汇）。
    supported_topologies: tuple[str, ...] = ()
    #: 资料出处。
    sources: tuple[Source, ...] = ()
    #: "sourced"（已按原厂资料核对）| "generic"（泛化料号/系列典型值）。
    review_state: str = "generic"
    #: 必要外围连接规则（数据手册要求的外围子图），见 parts/rules.py。
    connection_rules: tuple = ()

    # ---- 查询 ---------------------------------------------------------------

    def pin(self, ref: str) -> Pin | None:
        """按脚号或脚名找引脚；找不到返回 None。"""
        for p in self.pins:
            if ref == p.number or ref == p.name:
                return p
        return None

    def pin_number(self, ref: str) -> str | None:
        """IR 里的引脚引用（编号或名称）→ 统一物理编号。查无返回 None。"""
        p = self.pin(ref)
        return p.number if p is not None else None

    def pin_ambiguity(self, ref: str) -> tuple[str, ...]:
        """返回脚名/脚号命中多个**不同**引脚的编号集合（歧义检测）。

        正常器件里引脚名与编号一一对应，不会歧义；但自定义目录可能写出
        「1 号脚叫 2、2 号脚也叫 2」这种表——此时必须拒绝，不能取第一个命中。
        """
        hits = {p.number for p in self.pins if ref == p.number or ref == p.name}
        return tuple(sorted(hits)) if len(hits) > 1 else ()

    def rating(self, key: str) -> float | None:
        """电气特性/能力值（容差、限流、耐压）。"""
        return dict(self.ratings).get(key)

    def limit(self, key: str) -> float | None:
        """推荐工作范围。"""
        return dict(self.operating).get(key)

    def absolute_max(self, key: str) -> float | None:
        """绝对最大值——判「超限」用这个，判「能否长期工作」用 limit()。"""
        return dict(self.abs_max).get(key)

    def supports_topology(self, topology: str) -> bool | None:
        """是否支持该拓扑；目录未声明时返回 None（=未知，不是 False）。"""
        if not self.supported_topologies:
            return None
        return topology in self.supported_topologies

    def footprints(self) -> tuple[str, ...]:
        """允许的封装集合；未显式给出时退化为默认封装单元素集合。"""
        if self.allowed_footprints:
            return self.allowed_footprints
        return (self.footprint,) if self.footprint else ()


# ---- 器件表 -----------------------------------------------------------------

#: 复核日期：本次核对的日期（不是文档发布日期）。
_R = "2026-09-26"
_R_PDF = (
    "本地原厂 PDF 副本 output/review-evidence/MP1584.pdf，"
    "SHA-256 647BE29D30E0EEF770039B1DB2B9EAC2FFF8816EE54FAC0954D12EF8BB940736"
)

MP1584_SOURCES = (
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="2", retrieved=_R,
        note="包装/顶视图脚位（1 SW,2 EN,3 COMP,4 FB,5 GND,6 FREQ,7 VIN,8 BST）；"
             "绝对最大值：VIN -0.3~+30V、SW -0.3~VIN+0.3V、BST-SW -0.3~+6V、"
             "All Other Pins -0.3~+6V；推荐工作：VIN 4.5~28V、VOUT 0.8~25V；"
             "θJA=50°C/W（SOIC8E，JESD51-7 四层板）",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="3", retrieved=_R,
        note="电气特性：VFB 0.776/0.8/0.824V；电流限流 4.0A(min)/4.7A(typ)；"
             "EN 上升阈值 1.35/1.5/1.65V、回差 300mV；"
             "RFREQ=100kΩ 时振荡频率 900kHz(typ)；软启动 1.5ms",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="4", retrieved=_R,
        note="PIN FUNCTIONS：EN 悬空即使能；BST 与 SW 之间接旁路电容；"
             "FREQ 对地接电阻设定频率；GND 行的 Exposed Pad 需接 GND 平面散热",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="9", retrieved=_R,
        note="可编程振荡器：Rfreq(kΩ) = 180000 / [fs(kHz)]^1.1",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="10", retrieved=_R,
        note="分压电阻：VOUT=VFB(1+R1/R2)，R2 需 <40kΩ（吸收 BS 电路约 20µA），"
             "典型 R2=40.2kΩ、R1=50.25×(VOUT-0.8) kΩ；电感公式与峰值电流公式",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="12,13", retrieved=_R,
        note="补偿：COMP 对地为串联 RC（图 2 另加可选 C6）；"
             "Table 3：VOUT=3.3V、L=6.8-10µH、C2=22µF → R3=68.1kΩ、C3=220pF、C6=None",
    ),
    Source(
        kind="datasheet", url="https://www.monolithicpower.cn/cn/documentview/"
        "productdocument/index/version/2/document_type/Datasheet/lang/en/"
        "sku/MP1584EN-LF-Z/document_id/204/",
        revision="MP1584 Rev 1.0 (8/8/2011)", pages="14", retrieved=_R,
        note="轻载脉冲跳跃时 VIN-VOUT 需 >3V，可用 EN 编程输入 UVLO 到 VOUT+3V",
    ),
    Source(
        kind="product_page", url="https://www.monolithicpower.com/en/mp1584.html",
        revision="产品页（状态查询）", pages="-", retrieved="2026-09-25",
        note="生命周期：不建议用于新设计（NRFND），替代型号 MP2338；"
             "仍为现有客户生产，因此记 nrfnd 而非 eol",
    ),
    Source(
        kind="footprint",
        url="KiCad 10.0.6 官方封装库 Package_SO.pretty/"
            "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm.kicad_mod",
        revision="KiCad 10.0.6", pages="-", retrieved=_R,
        note="电气焊盘为 1-8 + pad 9（2.41×3.3mm 居中矩形，"
             "pad_prop_heatsink，zone_connect=2）；另有 4 个仅 F.Paste 的"
             "无名焊盘（钢网开窗，非电气焊盘）——散热焊盘对应 pad 9",
    ),
)

#: 器件表。IR 里 components[].part 必须命中这里。
PARTS: dict[str, Part] = {
    # ---- 稳压器 ----
    "MP1584EN": Part(
        name="MP1584EN", category="regulator", package="SOIC-8E (SOIC-8 + EP)",
        spec="DC-DC 降压（异步），4.5–28V 输入，0.8–25V 输出，3A，"
             "100kHz–1.5MHz 可编程，SOIC8E 带散热焊盘",
        lifecycle="nrfnd", lib_id=None,
        manufacturer="Monolithic Power Systems (MPS)", mpn="MP1584EN-LF-Z",
        footprint="Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm",
        allowed_footprints=(
            "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm",
        ),
        supported_topologies=("async_buck",),
        pins=(
            # 脚号/脚名/电气类型全部来自原厂第 2、4 页（脚号 = 封装焊盘号）
            Pin("1", "SW", "power_out"),      # 高边开关输出（手册第 4 页）
            Pin("2", "EN", "input", required=True,
                nc_condition="手册第 4 页：EN 悬空即使能芯片；"
                             "仅在不要求上电时序/UVLO 编程时可用"),
            Pin("3", "COMP", "passive"),      # 误差放大器输出，必须接补偿支路
            Pin("4", "FB", "input"),
            Pin("5", "GND", "power_in"),
            Pin("6", "FREQ", "passive"),      # 必须对地接电阻设定频率
            Pin("7", "VIN", "power_in"),
            Pin("8", "BST", "passive"),       # 必须与 SW 之间接自举电容
            Pin("9", "EP", "power_in"),       # 散热焊盘 = footprint pad 9 = GND
        ),
        # 推荐工作条件（手册第 2 页）；绝对最大值单列，二者不可混用
        operating=(("vin_min", 4.5), ("vin_max", 28.0),
                   ("vout_min", 0.8), ("vout_max", 25.0),
                   ("iout_max", 3.0)),
        # SW 的绝对值是「VIN+0.3V」，不是固定数，故不写成常数，只记在来源里
        abs_max=(("vin", 30.0), ("bst_sw", 6.0), ("other_pins", 6.0),
                 ("junction_temp_c", 150.0)),
        ratings=(("vfb_min", 0.776), ("vfb_typ", 0.8), ("vfb_max", 0.824),
                 ("rds_on_ohm", 0.150),   # 第 3 页，VBST-VSW=5V
                 ("current_limit_min", 4.0), ("theta_ja_c_per_w", 50.0),
                 ("freq_min_hz", 100e3), ("freq_max_hz", 1500e3),
                 ("en_threshold_typ", 1.5), ("en_hysteresis", 0.3),
                 ("soft_start_s", 1.5e-3)),
        sources=MP1584_SOURCES,
        review_state="sourced",
        price_cny=1.2, stock=5000,
        alternatives=("MP2338",),
        # 数据手册要求的外围子图（检查器 checks/subgraph.py）。
        # 每条的 evidence 都是能翻到的页码——判错时要能指给人看。
        connection_rules=(
            CapBetween(
                pin_a="BST", pin_b="SW",
                evidence="MP1584 Rev1.0 第 4 页：BST 与 SW 之间接自举旁路电容"),
            RToNet(
                pin="FREQ", net="GND",
                evidence="MP1584 Rev1.0 第 4/9 页：FREQ 对地接电阻设定开关频率；"
                         "Rfreq(kΩ)=180000/fs(kHz)^1.1"),
            SeriesRCToNet(
                pin="COMP", net="GND",
                evidence="MP1584 Rev1.0 第 12/13 页图 2 与 Table 3：COMP 对地为"
                         "电阻串联电容（可选 C6 另加，不替代串联 RC）"),
            NotTiedToPin(
                pin="EN", other_pin="VIN",
                why="EN 与输入脚同网会把 EN 顶到输入电压上；该脚绝对最大值只有 "
                    "6V（第 2 页 All Other Pins），输入上限 28V，直接相接会损坏",
                evidence="MP1584 Rev1.0 第 2 页绝对最大值、第 1/14 页 EN 分压用法"),
        ),
    ),
    "AMS1117-3.3": Part(
        name="AMS1117-3.3", category="regulator", package="SOT-223",
        spec="LDO，固定 3.3V 输出，最大 1A，压差约 1.1V（泛化料号）",
        lifecycle="active", lib_id=None,
        footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
        allowed_footprints=("Package_TO_SOT_SMD:SOT-223-3_TabPin2",),
        supported_topologies=("ldo",),
        pins=(Pin("1", "GND", "power_in"),
              Pin("2", "VOUT", "power_out"),
              Pin("3", "VIN", "power_in")),
        ratings=(("vout_typ", 3.3),),
        operating=(("iout_max", 1.0),),
        review_state="generic",   # 未落到厂牌 MPN，未核对压差/热阻曲线
        price_cny=0.15, stock=80000,
    ),

    # ---- 电感 ----
    "IND-POW-10uH-3A": Part(
        name="IND-POW-10uH-3A", category="inductor", package="6.3x6.3",
        spec="功率电感 10µH，饱和电流 ≥3A（泛化料号，DCR/Isat 待落到实际 MPN）",
        lifecycle="active", lib_id="Device:L",
        footprint="Inductor_SMD:L_6.3x6.3_H3",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("inductance", 10e-6, "H"),
        ratings=(("current_a", 3.0),),
        review_state="generic",
        price_cny=0.6, stock=20000,
    ),
    "IND-POW-4.7uH-3A": Part(
        name="IND-POW-4.7uH-3A", category="inductor", package="6.3x6.3",
        spec="功率电感 4.7µH，饱和电流 ≥3A（泛化料号）",
        lifecycle="active", lib_id="Device:L",
        footprint="Inductor_SMD:L_6.3x6.3_H3",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("inductance", 4.7e-6, "H"),
        ratings=(("current_a", 3.0),),
        review_state="generic",
        price_cny=0.55, stock=20000,
    ),

    # ---- 电容 ----
    "CAP-0805-10uF-25V-X5R": Part(
        name="CAP-0805-10uF-25V-X5R", category="capacitor", package="0805",
        spec="MLCC 10µF/25V X5R（泛化料号；12V 偏置下直流偏压降容显著，"
             "实际可用容量需按厂牌曲线核对）",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0805_2012Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("capacitance", 10e-6, "F"),
        ratings=(("voltage_v", 25.0),),
        review_state="generic",
        price_cny=0.05, stock=100000,
    ),
    "CAP-1206-22uF-25V-X5R": Part(
        name="CAP-1206-22uF-25V-X5R", category="capacitor", package="1206",
        spec="MLCC 22µF/25V X5R（泛化料号）",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_1206_3216Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("capacitance", 22e-6, "F"),
        ratings=(("voltage_v", 25.0),),
        review_state="generic",
        price_cny=0.08, stock=100000,
    ),
    "CAP-0402-0.1uF-25V": Part(
        name="CAP-0402-0.1uF-25V", category="capacitor", package="0402",
        spec="MLCC 0.1µF/25V（泛化料号；高频旁路/自举用）",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("capacitance", 0.1e-6, "F"),
        ratings=(("voltage_v", 25.0),),
        review_state="generic",
        price_cny=0.01, stock=500000,
    ),
    "CAP-0402-10nF-25V": Part(
        name="CAP-0402-10nF-25V", category="capacitor", package="0402",
        spec="MLCC 10nF/25V（泛化料号）",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("capacitance", 10e-9, "F"),
        ratings=(("voltage_v", 25.0),),
        review_state="generic",
        price_cny=0.01, stock=500000,
    ),
    "CAP-0402-220pF-50V": Part(
        name="CAP-0402-220pF-50V", category="capacitor", package="0402",
        spec="MLCC 220pF/50V C0G（泛化料号；补偿电容用，C0G 温度稳定）",
        lifecycle="active", lib_id="Device:C",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("capacitance", 220e-12, "F"),
        ratings=(("voltage_v", 50.0),),
        review_state="generic",
        price_cny=0.01, stock=500000,
    ),

    # ---- 电阻（0603 厚膜，1%）----
    "RES-0603-49.9k-1%": Part(
        name="RES-0603-49.9k-1%", category="resistor", package="0603",
        spec="厚膜 49.9kΩ ±1%（泛化料号；系列典型额定 0.1W/75V）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 49.9e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-16k-1%": Part(
        name="RES-0603-16k-1%", category="resistor", package="0603",
        spec="厚膜 16kΩ ±1%（泛化料号）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 16e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-3.3k-1%": Part(
        name="RES-0603-3.3k-1%", category="resistor", package="0603",
        spec="厚膜 3.3kΩ ±1%（泛化料号）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 3.3e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-24.9k-1%": Part(
        name="RES-0603-24.9k-1%", category="resistor", package="0603",
        spec="厚膜 24.9kΩ ±1%（泛化料号）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 24.9e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-124k-1%": Part(
        name="RES-0603-124k-1%", category="resistor", package="0603",
        spec="厚膜 124kΩ ±1%（泛化料号；MP1584 手册第 1 页典型应用 FB 上臂）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 124e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-40.2k-1%": Part(
        name="RES-0603-40.2k-1%", category="resistor", package="0603",
        spec="厚膜 40.2kΩ ±1%（泛化料号；MP1584 手册第 10 页推荐 FB 下臂）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 40.2e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-68.1k-1%": Part(
        name="RES-0603-68.1k-1%", category="resistor", package="0603",
        spec="厚膜 68.1kΩ ±1%（泛化料号；MP1584 补偿电阻 Table 3 值）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 68.1e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-100k-1%": Part(
        name="RES-0603-100k-1%", category="resistor", package="0603",
        spec="厚膜 100kΩ ±1%（泛化料号；MP1584 典型应用 EN 分压上臂）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 100e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),
    "RES-0603-200k-1%": Part(
        name="RES-0603-200k-1%", category="resistor", package="0603",
        spec="厚膜 200kΩ ±1%（泛化料号；MP1584 FREQ 设定电阻）",
        lifecycle="active", lib_id="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        nominal=Param("resistance", 200e3, "ohm"),
        ratings=(("resistance_tolerance", 0.01), ("power_w", 0.1),
                 ("voltage_v", 75.0)),
        review_state="generic",
        price_cny=0.008, stock=200000,
    ),

    # ---- 二极管 / 接插件 ----
    "SS34": Part(
        name="SS34", category="diode", package="SMA",
        spec="肖特基二极管 3A/40V（buck 续流用；通用货号，未落到厂牌 MPN）",
        lifecycle="active", lib_id="Device:D_Schottky",
        footprint="Diode_SMD:D_SMA",
        pins=(Pin("1", "K", "passive"), Pin("2", "A", "passive")),
        ratings=(("current_a", 3.0), ("voltage_v", 40.0)),
        review_state="generic",
        price_cny=0.3, stock=30000,
    ),
    "XH2.54-2P": Part(
        name="XH2.54-2P", category="connector", package="XH-2P-2.54mm",
        spec="JST XH 2.54mm 2P 接插件（电源输入/输出端子；通用货号）",
        lifecycle="active", lib_id="Connector_Generic:Conn_01x02",
        footprint="Connector_JST:JST_XH_B2B-XH-AM_1x02_P2.50mm_Vertical",
        pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
        review_state="generic",
        price_cny=0.1, stock=50000,
    ),
}


def find(name: str) -> Part | None:
    """按型号查器件，查无返回 None。"""
    return PARTS.get(name)


def resolve_pin(part: Part, pin: str) -> str | None:
    """IR 里的引脚引用（编号或名称）→ 统一引脚编号。查无返回 None。"""
    return part.pin_number(pin)
