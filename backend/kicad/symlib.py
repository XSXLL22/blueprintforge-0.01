"""官方 KiCad 符号库的解析与内嵌。

血泪结论（详见 output/probe5 实验）：kicad-cli 10.x 跑 ERC/导网表时，
符号定义只认 .kicad_sch 里 (lib_symbols) 内嵌的副本，不查 sym-lib-table
——表里写了对的库、绝对路径、环境变量 URI 全都没用，官方符号引脚全部
悬空、类型 Unspecified，ERC 却照样「通过」。所以这条路线改成：

- 把要用的官方符号定义从随 KiCad 附带的官方库抽取出来、随仓库发行
  （vendor 在 backend/kicad/symbols/，来源 KiCad 10.0.6 官方符号库，
  CC-BY-SA-4.0，见该目录 README.md）；
- 渲染原理图时把定义整体内嵌进 (lib_symbols)，实例照常引用 lib_id。
  这样输出完全自包含：不装 KiCad 的机器也能生成同样的文件。

符号块里的引脚几何（编号/名称/电气类型/坐标/角度）与器件本体范围也从
vendor 文件里解析出来——单一数据源，杜绝硬编码表与官方库漂移。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: vendor 目录。
SYMBOLS_DIR = Path(__file__).parent / "symbols"

#: 用到的官方符号：lib_id → (vendor 文件名, 符号名)。
OFFICIAL: dict[str, tuple[str, str]] = {
    "Device:R": ("Device.kicad_sym", "R"),
    "Device:C": ("Device.kicad_sym", "C"),
    "Device:L": ("Device.kicad_sym", "L"),
    "Device:D_Schottky": ("Device.kicad_sym", "D_Schottky"),
    "power:GND": ("power.kicad_sym", "GND"),
    "power:PWR_FLAG": ("power.kicad_sym", "PWR_FLAG"),
    "Connector_Generic:Conn_01x02": ("Connector_Generic.kicad_sym",
                                      "Conn_01x02"),
}


@dataclass(frozen=True)
class Pin:
    """符号的一个引脚（库坐标，Y 轴朝上）。

    angle 是「从连接点指向本体内部」的方向：0 右 / 90 上 / 180 左 / 270 下，
    导线残端要朝反方向（远离本体）延伸。
    """
    number: str
    name: str
    kind: str          # passive / power_in / power_out / no_connect ...
    x: float
    y: float
    angle: int
    length: float


def _block(text: str, start: int) -> str:
    """从 text[start] 的 '(' 配平到对应 ')'，返回整块。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError(f"括号不配平（位置 {start}）")


def symbol_block(lib_id: str) -> str:
    """vendor 库中该符号的原始 (symbol ...) 块。"""
    fname, name = OFFICIAL[lib_id]
    path = SYMBOLS_DIR / fname
    if not path.is_file():
        raise RuntimeError(
            f"vendor 符号库缺失：{path}——仓库不完整，拒绝渲染"
            f"（{lib_id} 需要 {fname} 里的 '{name}'）")
    text = path.read_text(encoding="utf-8")
    try:
        start = text.index(f'(symbol "{name}"')
    except ValueError:
        raise RuntimeError(f"vendor 库 {fname} 里找不到符号 '{name}'")
    return _block(text, start)


def pins_of(lib_id: str) -> tuple[Pin, ...]:
    """符号的引脚几何（按库文件里的顺序）。"""
    block = symbol_block(lib_id)
    pins: list[Pin] = []
    for m in re.finditer(r"\(pin\s+", block):
        sub = _block(block, m.start())
        head = re.match(r"\(pin\s+(\S+)\s+(\S+)", sub)
        kind = head.group(1)
        at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(\d+)", sub)
        length = re.search(r"\(length\s+(-?[\d.]+)", sub)
        name = re.search(r'\(name\s+"([^"]*)"', sub)
        number = re.search(r'\(number\s+"([^"]*)"', sub)
        pins.append(Pin(
            number=number.group(1) if number else "",
            name=name.group(1) if name else "",
            kind=kind,
            x=float(at.group(1)) if at else 0.0,
            y=float(at.group(2)) if at else 0.0,
            angle=int(at.group(3)) if at else 0,
            length=float(length.group(1)) if length else 0.0,
        ))
    return tuple(pins)


def body_size(lib_id: str) -> tuple[float, float]:
    """符号本体+引脚锚点的总跨度 (宽, 高)（库坐标）。

    收集块内所有 (xy ...) 图形点与引脚 (at ...) 锚点；property 里只有
    (at ...) 没有 (xy ...)，所以文字不会撑大本体。
    """
    block = symbol_block(lib_id)
    xs = [float(m) for m in re.findall(r"\(xy\s+(-?[\d.]+)\s+-?[\d.]+\)",
                                       block)]
    ys = [float(m) for m in re.findall(
        r"\(xy\s+-?[\d.]+\s+(-?[\d.]+)\)", block)]
    for pin in pins_of(lib_id):
        xs.append(pin.x)
        ys.append(pin.y)
    return max(xs) - min(xs), max(ys) - min(ys)


def body_height(lib_id: str) -> float:
    """符号本体+引脚在 Y 方向的总跨度。"""
    return body_size(lib_id)[1]


def body_width(lib_id: str) -> float:
    """符号本体+引脚在 X 方向的总跨度。"""
    return body_size(lib_id)[0]


def ref_pos(lib_id: str) -> tuple[float, float]:
    """符号自带的 Reference 属性位置（库坐标），供电源符号摆位号用。"""
    block = symbol_block(lib_id)
    m = re.search(r'\(property\s+"Reference"[^\n]*\n'
                  r'((?:[^\n]*\n)*?)\s*\)', block)
    at = re.search(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+\d+\)",
                   m.group(0)) if m else None
    return (float(at.group(1)), float(at.group(2))) if at else (0.0, 0.0)


def embed(lib_id: str, indent: str) -> list[str]:
    """内嵌块：把库里的 (symbol "NAME" ...) 改名成 (symbol "LIB_ID" ...)。

    返回按 indent 缩进的逐行文本，直接拼进 (lib_symbols)。
    """
    block = symbol_block(lib_id)
    _, name = OFFICIAL[lib_id]
    block = block.replace(f'(symbol "{name}"', f'(symbol "{lib_id}"', 1)
    return [indent + line if line.strip() else line
            for line in block.splitlines()]
