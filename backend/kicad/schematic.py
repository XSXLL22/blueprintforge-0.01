"""IR → KiCad 原理图（.kicad_sch）。

符号策略（两种混用，全部内嵌进 lib_symbols——kicad-cli 只认内嵌符号）：
- IC（如 MP1584EN）：内嵌自绘方框符号（lib_id=None 的器件），继承
  BlueprintForge hdc/pcb/schematic.py 的做法；
- 无源器件/二极管/接插件/电源符号：引用官方 lib_id（Device:R 等），同时
  把官方符号定义从 vendor 库（backend/kicad/symbols/）内嵌进本文件，
  引脚几何也从 vendor 定义解析（symlib），单一数据源。

连接方式（三种网络三种机制，全部经 kicad-cli 实测验证）：
- 信号网络：每个引脚伸一小段导线 + 同名本地标签，KiCad 按标签同名合并；
- GND：导线残端放官方 power:GND 电源符号——`(power global)` 符号的
  power_in 引脚按符号 Value 自动并入全局 GND 网（KiCad 7+ 机制）；
- 其它 netclass=power 的网络：每个引脚残端放**现场生成的**同名电源符号
  `circuitos:PWR_<网络名>`（power_in + (power global)，模板抄官方 GND），
  靠 Value 自动全局合并——PWR_FLAG 做不到这一点（其引脚名是空串，
  只能靠导线接触合并，多引脚电源网络会被拆散成单引脚网，血的教训）；
- 驱动标记：IR 引脚里没有 power_out 的网络（含 GND）在图纸顶部放一个
  「PWR_FLAG 岛」——短导线 + PWR_FLAG + 该网络电源符号，告诉 ERC 该网络
  有驱动。网络里已有 power_out 引脚（如 U1 的 SW）则不放，避免
  power_out 对 power_out 冲突。

没接线的引脚放 no_connect。纯函数 render(ir) → str，同样输入必得同样输出。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from pathlib import Path

from backend.kicad import symlib
from ir.schema import PowerIR
from parts.partsdb import PARTS, Part

#: KiCad 10 官方格式戳（来自随安装附带的 demo 工程）。
SCH_VERSION = "20250610"
GENERATOR = "circuitos"
GENERATOR_VERSION = "0.1"

#: 网格 1.27mm —— 引脚、导线、标签必须落在同一网格上才能连上。
GRID = 1.27
PITCH = 2 * GRID          # 引脚行距
PIN_LEN = 2 * GRID        # 引脚线长
STUB = 3 * GRID           # 引脚到标签的导线长度
MARGIN = 10 * GRID        # 图纸边距
FONT = 1.27               # 文字高度
CHAR_W = 0.85             # 估算文字宽度用（KiCad 默认字体约 0.85·高）

#: 图纸尺寸（横向，mm）。从小到大试，取第一个装得下的。
PAPERS = (("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0),
          ("A1", 841.0, 594.0), ("A0", 1189.0, 841.0))

#: 名字空间，把「位号/符号名」映射成稳定的 UUID（保证输出可重复）。
_NS = uuid.UUID("9b3a7c51-2e4d-4f6a-8b90-c1d2e3f4a5b6")

GND_LIB_ID = "power:GND"
PWR_FLAG_LIB_ID = "power:PWR_FLAG"

#: 生成的电源符号 lib_id 前缀（写入 circuitos.kicad_sym + sym-lib-table）。
PWR_LIB = "circuitos"

#: 导线方向向量（原理图坐标，Y 轴向下）。
_AWAY_VEC = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


def _q(text: str) -> str:
    """按 s-expression 规则转义字符串（换行写成字面 \\n，KiCad 就是这么存的）。"""
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + out.replace("\n", "\\n") + '"'


def _num(value: float) -> str:
    return f"{round(value, 4):g}"


# ---- 符号几何 ---------------------------------------------------------------

@dataclass(frozen=True)
class _Sym:
    """一个内嵌生成的方框符号：两列引脚，标功能名与编号。"""

    name: str
    value: str
    footprint: str
    #: (编号, 名称, 电气类型)，按数据手册顺序
    pins: tuple[tuple[str, str, str], ...]
    half_width: float

    @property
    def rows(self) -> int:
        return math.ceil(len(self.pins) / 2)

    @property
    def top(self) -> float:
        return (self.rows - 1) * PITCH / 2 + GRID

    def xy(self, index: int) -> tuple[float, float]:
        """引脚连接点的库坐标（Y 轴朝上）。左半引脚在左侧，右半在右侧。"""
        side = -1.0 if index < self.rows else 1.0
        row = index if index < self.rows else len(self.pins) - 1 - index
        return side * (self.half_width + PIN_LEN), self.top - GRID - row * PITCH

    @property
    def height(self) -> float:
        return 2 * self.top


def _half_width(names: list[str]) -> float:
    longest = max((len(n) for n in names), default=1)
    needed = 2 * longest * CHAR_W + PITCH
    return max(5 * GRID, math.ceil(needed / 2 / GRID) * GRID)


def _symbol_of(part: Part) -> _Sym:
    return _Sym(
        name=part.name, value=part.name, footprint=part.footprint or "",
        pins=part.pins,
        half_width=_half_width([n for _, n, _ in part.pins]),
    )


def _v_reach(part: Part) -> float:
    """元件中心到最远竖直电气点的距离（引脚锚点或竖直残端）。

    竖直残端只出现在指向角 90/270（水平伸进本体）的引脚上；水平残端的
    引脚垂直范围就到锚点。同列上下两个元件的间距必须 ≥ 两者 v_reach
    之和 + GRID，否则残端导线会重叠、不同网络被 KiCad 合并。
    """
    if part.lib_id is not None:
        v = 0.0
        for p in symlib.pins_of(part.lib_id):
            ext = abs(p.y) + (STUB if p.angle in (90, 270) else 0.0)
            v = max(v, ext)
        return max(v, symlib.body_height(part.lib_id) / 2)
    return _symbol_of(part).height / 2


def _h_reach(part: Part) -> float:
    """元件中心到最远水平电气点的距离（引脚锚点或水平残端）。"""
    if part.lib_id is not None:
        h = 0.0
        for p in symlib.pins_of(part.lib_id):
            ext = abs(p.x) + (STUB if p.angle in (0, 180) else 0.0)
            h = max(h, ext)
        return max(h, symlib.body_width(part.lib_id) / 2)
    sym = _symbol_of(part)
    return sym.half_width + PIN_LEN + STUB


def _pin_rows(part: Part) -> tuple[tuple[str, str, float, float, int], ...]:
    """器件的引脚几何行：(编号, 名称, x, y, 指向角)。"""
    if part.lib_id is not None:
        if part.lib_id not in symlib.OFFICIAL:
            raise ValueError(
                f"器件 '{part.name}' 的符号 '{part.lib_id}' 不在 vendor 库里——"
                f"要么往 symlib.OFFICIAL 补充并 vendor 该符号，"
                f"要么把 lib_id 置 None 用内嵌符号")
        return tuple((p.number, p.name, p.x, p.y, p.angle)
                     for p in symlib.pins_of(part.lib_id))
    sym = _symbol_of(part)
    return tuple(
        (num, name, *sym.xy(i), 0 if sym.xy(i)[0] < 0 else 180)
        for i, (num, name, _) in enumerate(part.pins)
    )


def _pin_number(part: Part, pin: str) -> str:
    """IR 里的引脚引用（编号或名称）→ 引脚编号。"""
    for num, name, _, _, _ in _pin_rows(part):
        if pin == num or pin == name:
            return num
    raise ValueError(
        f"器件 '{part.name}' 没有名为/编号为 '{pin}' 的引脚"
        f"（可用: {[r[0] for r in _pin_rows(part)]}）")


# ---- 布图 -------------------------------------------------------------------

@dataclass(frozen=True)
class _Placed:
    ref: str
    part: Part
    x: float
    y: float
    rows: tuple[tuple[str, str, float, float, int], ...]

    @property
    def v_reach(self) -> float:
        return _v_reach(self.part)

    @property
    def h_reach(self) -> float:
        return _h_reach(self.part)


def _label_room(ir: PowerIR) -> float:
    longest = max((len(n.name) for n in ir.nets), default=4)
    return math.ceil(longest * CHAR_W / GRID) * GRID + PITCH


def _columns(items: list[_Placed], usable_h: float) -> list[list[int]]:
    cols: list[list[int]] = [[]]
    used = 0.0
    for index, it in enumerate(items):
        step = 2 * it.v_reach + GRID
        if cols[-1] and used + step > usable_h:
            cols.append([])
            used = 0.0
        cols[-1].append(index)
        used += step
    return cols


def _notes(ir: PowerIR) -> tuple[tuple[str, str, str], ...]:
    """图纸上的说明文字：(uuid 标签, 标题, 正文)。"""
    out: list[tuple[str, str, str]] = []
    if ir.design_rationale.strip():
        out.append(("rationale", "设计理由", ir.design_rationale))
    body = []
    if ir.assumptions:
        body.append("假设：" + "；".join(ir.assumptions))
    if ir.risks:
        body.append("风险：" + "；".join(ir.risks))
    if body:
        out.append(("assump", "假设与风险", "\n".join(body)))
    return tuple(out)


def _notes_band(notes: tuple[tuple[str, str, str], ...]) -> float:
    """图纸顶部说明区的估算高度（与 render 里实际绘制用同一公式）。"""
    band = 2 * PITCH
    for _, _, text in notes:
        band += (math.ceil(len(text) / 70) + 2) * FONT * 1.6
    return band


def _plan(ir: PowerIR, items: list[_Placed],
          parts: dict[str, Part]
          ) -> tuple[str, float, float, list[_Placed], float]:
    """挑一张装得下的图纸，算出每个元件的坐标；band 是图纸顶部说明区高度。"""
    notes = _notes(ir)
    band = _notes_band(notes) + _island_offset(ir, parts)

    room = _label_room(ir)
    h_reach = max(it.h_reach for it in items)
    col_w = 2 * (h_reach + room) + PITCH

    paper, width, height = PAPERS[-1]
    cols = [[i for i in range(len(items))]]
    for name, w, h in PAPERS:
        cols = _columns(items, h - 2 * MARGIN - band)
        if len(cols) * col_w <= w - 2 * MARGIN:
            paper, width, height = name, w, h
            break

    placed: list[_Placed] = []
    for col_index, col in enumerate(cols):
        cursor = MARGIN + band
        cx = MARGIN + room + h_reach + col_index * col_w
        for index in col:
            it = items[index]
            placed.append(_Placed(it.ref, it.part,
                                  round(cx / GRID) * GRID,
                                  round((cursor + it.v_reach) / GRID) * GRID,
                                  it.rows))
            cursor += 2 * it.v_reach + GRID
    return paper, width, height, placed, band


# ---- s-expression 输出 -------------------------------------------------------

def _effects(indent: str, *, hide: bool = False, justify: str = "") -> list[str]:
    out = [f"{indent}(effects",
           f"{indent}\t(font (size {_num(FONT)} {_num(FONT)}))"]
    if justify:
        out.append(f"{indent}\t(justify {justify})")
    if hide:
        out.append(f"{indent}\t(hide yes)")
    out.append(f"{indent})")
    return out


def _property(indent: str, key: str, value: str, x: float, y: float,
              *, hide: bool = False) -> list[str]:
    out = [f"{indent}(property {_q(key)} {_q(value)}",
           f"{indent}\t(at {_num(x)} {_num(y)} 0)"]
    out += _effects(indent + "\t", hide=hide)
    out.append(f"{indent})")
    return out


def _lib_symbol(sym: _Sym) -> list[str]:
    top = sym.top
    out = [f"\t\t(symbol {_q('circuitos:' + sym.name)}",
           "\t\t\t(pin_names (offset 0.508))",
           "\t\t\t(exclude_from_sim no)", "\t\t\t(in_bom yes)",
           "\t\t\t(on_board yes)"]
    out += _property("\t\t\t", "Reference", "U", 0, top + PITCH)
    out += _property("\t\t\t", "Value", sym.value, 0, -top - PITCH)
    out += _property("\t\t\t", "Footprint", sym.footprint, 0, 0, hide=True)
    out += _property("\t\t\t", "Datasheet", "", 0, 0, hide=True)
    out += _property("\t\t\t", "Description",
                     f"{sym.name}（{len(sym.pins)} 脚，circuitos 内嵌符号）",
                     0, 0, hide=True)
    out.append(f"\t\t\t(symbol {_q(sym.name + '_1_1')}")
    out.append(f"\t\t\t\t(rectangle (start {_num(-sym.half_width)} {_num(top)})"
               f" (end {_num(sym.half_width)} {_num(-top)})")
    out.append("\t\t\t\t\t(stroke (width 0.254) (type default))")
    out.append("\t\t\t\t\t(fill (type background)))")
    for index, (num, name, kind) in enumerate(sym.pins):
        x, y = sym.xy(index)
        angle = 0 if x < 0 else 180
        out.append(f"\t\t\t\t(pin {kind} line (at {_num(x)} {_num(y)} {angle})"
                   f" (length {_num(PIN_LEN)})")
        out.append(f"\t\t\t\t\t(name {_q(name)}")
        out += _effects("\t\t\t\t\t\t")
        out.append("\t\t\t\t\t)")
        out.append(f"\t\t\t\t\t(number {_q(num)}")
        out += _effects("\t\t\t\t\t\t")
        out.append("\t\t\t\t\t)")
        out.append("\t\t\t\t)")
    out.append("\t\t\t)")
    out.append("\t\t\t(embedded_fonts no)")
    out.append("\t\t)")
    return out


def _instance(place: _Placed, root: str, project: str, ir: PowerIR) -> list[str]:
    part = place.part
    lib_id = f"circuitos:{part.name}" if part.lib_id is None else part.lib_id
    value = next((c.value for c in ir.components if c.ref == place.ref), None) \
        or part.name
    out = ["\t(symbol",
           f"\t\t(lib_id {_q(lib_id)})",
           f"\t\t(at {_num(place.x)} {_num(place.y)} 0)",
           "\t\t(unit 1)",
           "\t\t(exclude_from_sim no)", "\t\t(in_bom yes)", "\t\t(on_board yes)",
           "\t\t(dnp no)",
           f"\t\t(uuid {_q(_uid('sym', place.ref))})"]
    if part.lib_id is None:
        sym = _symbol_of(part)
        out += _property("\t\t", "Reference", place.ref,
                         place.x, place.y - sym.top - PITCH)
        out += _property("\t\t", "Value", value,
                         place.x, place.y + sym.top + PITCH)
    else:
        out += _property("\t\t", "Reference", place.ref,
                         place.x, place.y - 2 * PITCH - GRID)
        out += _property("\t\t", "Value", value,
                         place.x, place.y + 2 * PITCH + GRID)
    out += _property("\t\t", "Footprint", part.footprint or "",
                     place.x, place.y, hide=True)
    out += _property("\t\t", "Datasheet", "", place.x, place.y, hide=True)
    for num, _, _, _, _ in place.rows:
        out.append(f"\t\t(pin {_q(num)} (uuid {_q(_uid('pin', place.ref, num))}))")
    out += ["\t\t(instances",
            f"\t\t\t(project {_q(project)}",
            f"\t\t\t\t(path {_q('/' + root)}",
            f"\t\t\t\t\t(reference {_q(place.ref)})", "\t\t\t\t\t(unit 1)",
            "\t\t\t\t)", "\t\t\t)", "\t\t)", "\t)"]
    return out


def _power_symbol(lib_id: str, value: str, ref: str, x: float, y: float,
                  key: str, root: str, project: str, ref_dy: float
                  ) -> list[str]:
    """GND/PWR_FLAG/PWR_<网络> 电源符号实例。位号隐藏（与官方定义一致）。"""
    out = ["\t(symbol",
           f"\t\t(lib_id {_q(lib_id)})",
           f"\t\t(at {_num(x)} {_num(y)} 0)",
           "\t\t(unit 1)",
           "\t\t(exclude_from_sim no)", "\t\t(in_bom yes)", "\t\t(on_board yes)",
           "\t\t(dnp no)",
           f"\t\t(uuid {_q(_uid(key))})"]
    out += _property("\t\t", "Reference", ref, x, y + ref_dy, hide=True)
    out += _property("\t\t", "Value", value, x, y + ref_dy)
    out.append(f"\t\t(pin \"1\" (uuid {_q(_uid(key + ':pin'))}))")
    out += ["\t\t(instances",
            f"\t\t\t(project {_q(project)}",
            f"\t\t\t\t(path {_q('/' + root)}",
            f"\t\t\t\t\t(reference {_q(ref)})", "\t\t\t\t\t(unit 1)",
            "\t\t\t\t)", "\t\t\t)", "\t\t)", "\t)"]
    return out


def _pwr_lib_id(net: str) -> str:
    """电源网络 <net> 的生成符号 lib_id。"""
    return f"{PWR_LIB}:PWR_{net}"


def _power_lib_symbol(net: str) -> list[str]:
    """生成 (symbol "circuitos:PWR_<net>") 电源符号定义。

    模板照抄官方 power.kicad_sym 的 GND：(power global) + 空名 power_in
    引脚 + Value=<net>——KiCad 7+ 按符号 Value 把实例并入全局网络，
    这正是 GND 符号能自动合并的机制（kicad-cli 实测）。本体是引脚上方的
    短横条（+5V 风格），与 GND 的下方横条区分。
    """
    lib_id = _pwr_lib_id(net)
    unit = f"PWR_{net}"
    head = [f"\t\t(symbol {_q(lib_id)}",
            "\t\t\t(power global)",
            "\t\t\t(pin_numbers",
            "\t\t\t\t(hide yes)",
            "\t\t\t)",
            "\t\t\t(pin_names",
            "\t\t\t\t(offset 0)",
            "\t\t\t\t(hide yes)",
            "\t\t\t)",
            "\t\t\t(exclude_from_sim no)",
            "\t\t\t(in_bom yes)",
            "\t\t\t(on_board yes)",
            "\t\t\t(in_pos_files no)",
            "\t\t\t(duplicate_pin_numbers_are_jumpers no)"]
    props = (_property("\t\t\t", "Reference", "#PWR", 0, -1.27, hide=True)
             + _property("\t\t\t", "Value", net, 0, 2.54)
             + _property("\t\t\t", "Footprint", "", 0, 0, hide=True)
             + _property("\t\t\t", "Datasheet", "", 0, 0, hide=True)
             + _property("\t\t\t", "Description",
                         f"电路网电源符号（circuitos 生成，连接全局网络 {net}）",
                         0, 0, hide=True))
    body = ["\t\t\t(symbol " + _q(unit + "_0_1"),
            "\t\t\t\t(polyline",
            "\t\t\t\t\t(pts",
            "\t\t\t\t\t\t(xy -1.27 1.27) (xy 1.27 1.27)",
            "\t\t\t\t\t)",
            "\t\t\t\t\t(stroke",
            "\t\t\t\t\t\t(width 0)",
            "\t\t\t\t\t\t(type default)",
            "\t\t\t\t\t)",
            "\t\t\t\t\t(fill",
            "\t\t\t\t\t\t(type none)",
            "\t\t\t\t\t)",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t\t(symbol " + _q(unit + "_1_1"),
            "\t\t\t\t(pin power_in line",
            "\t\t\t\t\t(at 0 0 90)",
            "\t\t\t\t\t(length 0)",
            "\t\t\t\t\t(name \"\"",
            "\t\t\t\t\t\t(effects",
            "\t\t\t\t\t\t\t(font",
            "\t\t\t\t\t\t\t\t(size 1.27 1.27)",
            "\t\t\t\t\t\t\t)",
            "\t\t\t\t\t\t)",
            "\t\t\t\t\t)",
            "\t\t\t\t\t(number \"1\"",
            "\t\t\t\t\t\t(effects",
            "\t\t\t\t\t\t\t(font",
            "\t\t\t\t\t\t\t\t(size 1.27 1.27)",
            "\t\t\t\t\t\t\t)",
            "\t\t\t\t\t\t)",
            "\t\t\t\t\t)",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t\t(embedded_fonts no)",
            "\t\t)"]
    return head + list(props) + body


def _connections(place: _Placed, net_of: dict[tuple[str, str], str],
                 power_nets: set[str],
                 root: str, project: str,
                 pwr_counter: list[int]) -> list[str]:
    """每个引脚：伸一段导线 + 标签/电源符号；没接线的放 no_connect。"""
    out: list[str] = []
    for num, _, lx, ly, angle in place.rows:
        px, py = place.x + lx, place.y - ly
        net = net_of.get((place.ref, num))
        if net is None:
            out += [f"\t(no_connect (at {_num(px)} {_num(py)})"
                    f" (uuid {_q(_uid('nc', place.ref, num))}))"]
            continue
        away = (angle + 180) % 360
        dx, dy = _AWAY_VEC[away]
        ex, ey = px + dx * STUB, py + dy * STUB
        out += [f"\t(wire (pts (xy {_num(px)} {_num(py)})"
                f" (xy {_num(ex)} {_num(ey)}))",
                "\t\t(stroke (width 0) (type default))",
                f"\t\t(uuid {_q(_uid('wire', place.ref, num))}))"]
        if net == "GND":
            pwr_counter[0] += 1
            out += _power_symbol(GND_LIB_ID, "GND", f"#PWR{pwr_counter[0]:03d}",
                                 ex, ey, f"gnd:{place.ref}:{num}", root, project,
                                 ref_dy=5 * GRID)
        elif net in power_nets:
            pwr_counter[0] += 1
            out += _power_symbol(_pwr_lib_id(net), net,
                                 f"#PWR{pwr_counter[0]:03d}",
                                 ex, ey, f"pwr:{net}:{place.ref}:{num}",
                                 root, project, ref_dy=4 * GRID)
        else:
            if away == 0:
                angle_t, just = 0, "left bottom"
            elif away == 180:
                angle_t, just = 180, "right bottom"
            elif away == 90:
                angle_t, just = 90, "left bottom"
            else:
                angle_t, just = 270, "right top"
            out.append(f"\t(label {_q(net)}")
            out.append(f"\t\t(at {_num(ex)} {_num(ey)} {angle_t})")
            out += _effects("\t\t", justify=just)
            out.append(f"\t\t(uuid {_q(_uid('label', place.ref, num))})")
            out.append("\t)")
    return out


# ---- 对外接口 ---------------------------------------------------------------

def _net_of_map(ir: PowerIR) -> dict[tuple[str, str], str]:
    """(位号, 引脚编号) → 网络名。引脚引用按编号或名称解析。"""
    m: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for pr in net.pins:
            comp = ir.component_map.get(pr.ref)
            if comp is None:
                continue
            part = PARTS.get(comp.part)
            if part is None:
                continue
            num = _pin_number(part, pr.pin)
            m[(pr.ref, num)] = net.name
    return m


def _is_power_net_class(ir: PowerIR, name: str) -> bool:
    for net in ir.nets:
        if net.name == name:
            return net.netclass == "power"
    return False


def _flagged_power_nets(ir: PowerIR, parts: dict[str, Part]) -> set[str]:
    """需要 PWR_FLAG 岛标驱动的电源网络：IR 引脚里没有 power_out 的网络。

    引脚电气类型以 partsdb 的 pins 元组 (编号, 名称, 类型) 为唯一事实来源
    （官方符号部件与自绘方框部件都带类型）。
    """
    driven: set[str] = set()
    pin_types: dict[str, dict[str, str]] = {}
    for net in ir.nets:
        for pr in net.pins:
            comp = ir.component_map.get(pr.ref)
            if comp is None or comp.part not in parts:
                continue
            part = parts[comp.part]
            if part.name not in pin_types:
                pin_types[part.name] = {num: kind
                                        for num, _, kind in part.pins}
            num = _pin_number(part, pr.pin)
            if pin_types[part.name].get(num) == "power_out":
                driven.add(net.name)
    return {n.name for n in ir.nets
            if n.netclass == "power" and n.name not in driven}


def _island_offset(ir: PowerIR, parts: dict[str, Part]) -> float:
    """有 PWR_FLAG 岛时图纸顶部多占一行的高度。"""
    flagged = _flagged_power_nets(ir, parts)
    return PITCH + GRID if flagged else 0.0


def render(ir: PowerIR, parts: dict[str, Part] | None = None) -> str:
    """把 IR 渲染成 .kicad_sch 文本。纯函数，同样输入必得同样输出。"""
    parts = PARTS if parts is None else parts
    items = [_Placed(c.ref, parts[c.part], 0.0, 0.0, _pin_rows(parts[c.part]))
             for c in ir.components]
    paper, _, _, placed, band = _plan(ir, items, parts)
    root = _uid("sheet", ir.project)
    net_of = _net_of_map(ir)
    power_nets = {n.name for n in ir.nets if n.netclass == "power"}
    flagged = _flagged_power_nets(ir, parts)
    pwr_counter = [0]

    out = ["(kicad_sch",
           f"\t(version {SCH_VERSION})",
           f"\t(generator {_q(GENERATOR)})",
           f"\t(generator_version {_q(GENERATOR_VERSION)})",
           f"\t(uuid {_q(root)})",
           f"\t(paper {_q(paper)})",
           "\t(title_block",
           f"\t\t(title {_q(ir.project + ' —— 电路AI自动生成')})",
           "\t\t(rev \"1\")",
           f"\t\t(company {_q('circuitos 自动生成 · 需人工复核后打样')})",
           "\t\t(date \"\")",
           "\t)"]

    embedded = sorted({c.part for c in ir.components
                       if parts[c.part].lib_id is None})
    official = {parts[c.part].lib_id for c in ir.components
                if parts[c.part].lib_id is not None}
    if any(n.name == "GND" for n in ir.nets):
        official.add(GND_LIB_ID)
    if flagged:
        official.add(PWR_FLAG_LIB_ID)
    pwr_nets = sorted(n.name for n in ir.nets
                      if n.netclass == "power" and n.name != "GND")
    out.append("\t(lib_symbols")
    for name in embedded:
        out += _lib_symbol(_symbol_of(parts[name]))
    for lib_id in sorted(official):
        out += symlib.embed(lib_id, "\t\t")
    for net in pwr_nets:
        out += _power_lib_symbol(net)
    out.append("\t)")

    y = MARGIN + PITCH + _island_offset(ir, parts)
    for tag, title, text in _notes(ir):
        out.append(f"\t(text {_q(title + '\\n' + text)}")
        out.append(f"\t\t(at {_num(MARGIN)} {_num(y)} 0)")
        out += _effects("\t\t", justify="left top")
        out.append(f"\t\t(uuid {_q(_uid('text', tag, ir.project))})")
        out.append("\t)")
        y += (math.ceil(len(text) / 70) + 2) * FONT * 1.6

    # PWR_FLAG 岛：没有 power_out 引脚的网络，在图纸顶部用短导线
    # + PWR_FLAG + 电源符号标一个驱动，让 ERC 知道该网络有人供电。
    for i, net in enumerate(sorted(flagged)):
        ix = MARGIN + i * 6 * GRID
        iy = MARGIN + GRID
        out.append(f"\t(wire (pts (xy {_num(ix)} {_num(iy)})"
                   f" (xy {_num(ix + 2 * GRID)} {_num(iy)}))")
        out.append("\t\t(stroke (width 0) (type default))")
        out.append(f"\t\t(uuid {_q(_uid('island-wire', net))}))")
        pwr_counter[0] += 1
        out += _power_symbol(PWR_FLAG_LIB_ID, "PWR_FLAG",
                             f"#PWR{pwr_counter[0]:03d}",
                             ix, iy, f"flag:{net}", root, ir.project,
                             ref_dy=3 * GRID)
        pwr_counter[0] += 1
        out += _power_symbol(
            GND_LIB_ID if net == "GND" else _pwr_lib_id(net), net,
            f"#PWR{pwr_counter[0]:03d}",
            ix + 2 * GRID, iy, f"island:{net}", root, ir.project,
            ref_dy=5 * GRID if net == "GND" else 4 * GRID)

    for place in placed:
        out += _connections(place, net_of, power_nets,
                            root, ir.project, pwr_counter)
    for place in placed:
        out += _instance(place, root, ir.project, ir)

    out += ["\t(sheet_instances",
            "\t\t(path \"/\"",
            "\t\t\t(page \"1\")",
            "\t\t)",
            "\t)",
            "\t(embedded_fonts no)",
            ")"]
    return "\n".join(out) + "\n"


def _symbol_lib_text(ir: PowerIR, parts: dict[str, Part]) -> str:
    """circuitos.kicad_sym 全文：自绘方框符号 + 生成的电源符号。

    与内嵌进 .kicad_sch 的定义同源。kicad-cli 只认内嵌副本，这个文件 +
    sym-lib-table 是给 KiCad 图形界面用的（也让 ERC 不再报「circuitos
    库不在配置中」，probe7 实测：有 .kicad_pro + 表 + 本文件即无警告）。
    """
    embedded = sorted({c.part for c in ir.components
                       if parts[c.part].lib_id is None})
    pwr_nets = sorted(n.name for n in ir.nets
                      if n.netclass == "power" and n.name != "GND")
    out = ["(kicad_symbol_lib",
           "\t(version 20251024)",
           "\t(generator \"circuitos\")",
           "\t(generator_version \"0.1\")"]
    for name in embedded:
        out += _lib_symbol(_symbol_of(parts[name]))
    for net in pwr_nets:
        out += _power_lib_symbol(net)
    out.append(")")
    return "\n".join(out) + "\n"


def write_schematic(ir: PowerIR, out_dir: Path) -> Path:
    """把原理图写到 <out_dir>/<project>.kicad_sch，返回路径。

    同时写配套的 circuitos.kicad_sym（自绘符号库，给 GUI 用）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ir.project}.kicad_sch"
    path.write_text(render(ir), encoding="utf-8")
    (out_dir / "circuitos.kicad_sym").write_text(
        _symbol_lib_text(ir, PARTS), encoding="utf-8")
    return path
