"""带单位的数值解析与比较。

为什么单独一个模块：器件的额定值、IR 的 `value`、设计目标都必须在**同一套
量纲**上比较，否则「49.9k 的料号标成 10k」这类矛盾没人能发现。规则：

- 一律转成 SI 基本单位（Ω / F / H / V / A / W / Hz）后比较；
  温度是唯一例外，用摄氏度（工程惯例），且**不套用「必须大于零」**。
- 解析失败返回 `None`，**绝不猜**。调用方把 None 变成结构化诊断，
  不能退化成 0.0 或默认值。
- 反斜杠/全角字符（`µ` U+00B5、`μ` U+03BC）都要认，这是常见输入。

支持写法示例（电阻）：`49.9k`、`4R7`、`1K5`、`10M`、`49.9kΩ`、`49900`
（电容）：`100nF`、`0.1uF`、`0.1µF`、`220pF`、`10u`
（电感）：`10uH`、`4.7µH`、`4.7u`
（目标量）：`50mV`、`0.05V`、`85%`、`10元`、`60`（配合期望量纲）
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

#: 十进前缀（含微符号的三种写法）。
PREFIXES: dict[str, float] = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}

#: 量纲 → (基本单位符号, 该量纲接受的后缀写法)。后缀不区分大小写，
#: 但前缀区分（m=毫，M=兆）。
DIMENSIONS: dict[str, dict] = {
    "resistance": {"unit": "Ω", "symbols": ("ohm", "Ω", "R", "Ohm")},
    "capacitance": {"unit": "F", "symbols": ("F", "f")},
    "inductance": {"unit": "H", "symbols": ("H", "h")},
    "voltage": {"unit": "V", "symbols": ("V", "v")},
    "current": {"unit": "A", "symbols": ("A", "a")},
    "power": {"unit": "W", "symbols": ("W", "w")},
    "frequency": {"unit": "Hz", "symbols": ("Hz", "hz", "HZ")},
    "temperature": {"unit": "°C", "symbols": ("C", "°C", "℃")},
    "time": {"unit": "s", "symbols": ("s",)},
    "ratio": {"unit": "", "symbols": ("%",)},
}

#: 无量纲比值（百分比、效率）→ 分数。例如 85% → 0.85。
_RATIO = {"%": 0.01}

#: 货币：只用于成本目标，统一按人民币元。
CURRENCY = {"cny": 1.0, "元": 1.0, "rmb": 1.0, "￥": 1.0, "¥": 1.0}

_NUM_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
#: 电阻简写：4R7 / 1K5 / 2M2（字母当小数点）
_SHORTHAND_RE = re.compile(
    r"^(?P<a>\d*)(?P<letter>[RrKkMm])(?P<b>\d+)$")


class ValueError_(ValueError):
    """数值解析失败。仅在调用方要求抛异常时使用。"""


@dataclass(frozen=True)
class Measurement:
    """一个解析成功的量：量纲 + SI 数值 + 原始文本。"""
    dimension: str
    value: float
    text: str

    def pretty(self) -> str:
        return format_magnitude(self.dimension, self.value)


def _finite(x: float) -> bool:
    return isinstance(x, float) and math.isfinite(x)


def _split_number(text: str) -> tuple[str, str] | None:
    """把 `12.5kΩ` 拆成 ('12.5', 'kΩ')；数字必须以数字、正负号或小数点开头。"""
    s = text.strip()
    if not s:
        return None
    m = re.match(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", s)
    if not m:
        return None
    return m.group(0), s[m.end():].strip()


def _split_prefix_unit(suffix: str, symbols: tuple[str, ...]) -> tuple[float, str] | None:
    """把 `kΩ` 拆成 (1000.0, 'Ω')。"""
    if suffix == "":
        return 1.0, ""
    for sym in sorted(symbols, key=len, reverse=True):   # 先匹配长后缀
        if suffix.endswith(sym) and sym:
            head = suffix[: -len(sym)]
            if head in PREFIXES:
                return PREFIXES[head], sym
    if suffix in PREFIXES:                                # 只有前缀，如 '10u'
        return PREFIXES[suffix], ""
    return None


def parse_magnitude(text: object, dimension: str | None = None) -> Measurement | None:
    """解析带单位的量。失败返回 None（不抛异常、不猜）。

    `dimension` 给定时，后缀必须与该量纲相容（无后缀也可以）；
    不给定时从后缀推断，无法判断就返回 None。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    # 电阻简写：4R7 → 4.7Ω、1K5 → 1.5kΩ、2M2 → 2.2MΩ
    m = _SHORTHAND_RE.match(raw)
    if m and dimension in (None, "resistance"):
        head, letter, tail = m.group("a"), m.group("letter"), m.group("b")
        mult = {"R": 1.0, "r": 1.0, "K": 1e3, "k": 1e3,
                "M": 1e6, "m": 1e-3}[letter]
        try:
            value = float(f"{head or '0'}.{tail}") * mult
        except ValueError:
            return None
        return Measurement("resistance", value, raw) if _finite(value) else None

    split = _split_number(raw)
    if split is None:
        return None
    number_text, suffix = split
    try:
        number = float(number_text)
    except ValueError:
        return None
    if not _finite(number):
        return None

    # 货币
    if suffix.lower() in CURRENCY and dimension in (None, "cost"):
        return Measurement("cost", number * CURRENCY[suffix.lower()], raw)

    # 百分比：85% → 0.85
    if suffix in _RATIO:
        return Measurement("ratio", number * _RATIO[suffix], raw)

    if suffix == "":
        if dimension is None:
            return None                     # 无单位又没给量纲 → 不猜
        return Measurement(dimension, number, raw)

    candidates = [dimension] if dimension else list(DIMENSIONS)
    for dim in candidates:
        if dim not in DIMENSIONS:
            continue
        got = _split_prefix_unit(suffix, DIMENSIONS[dim]["symbols"])
        if got is None:
            continue
        mult, _sym = got
        value = number * mult
        if not _finite(value):
            return None
        return Measurement(dim, value, raw)
    return None


def parse_scalar(text: object) -> float | None:
    """解析未带单位、但必须是有限数的输入（如 JSON 里已是数值的目标）。"""
    if isinstance(text, bool) or not isinstance(text, (int, float)):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def format_magnitude(dimension: str, value: float, *, digits: int = 4) -> str:
    """把 SI 值格式化成人看的样子（10uH、49.9kΩ、50mV）。"""
    if not _finite(value):
        return "?"
    unit = DIMENSIONS.get(dimension, {}).get("unit", "")
    table = {
        "resistance": [(1e6, "MΩ"), (1e3, "kΩ"), (1.0, "Ω")],
        "capacitance": [(1e-3, "mF"), (1e-6, "µF"), (1e-9, "nF"), (1e-12, "pF")],
        "inductance": [(1.0, "H"), (1e-3, "mH"), (1e-6, "µH"), (1e-9, "nH")],
        "voltage": [(1.0, "V"), (1e-3, "mV")],
        "current": [(1.0, "A"), (1e-3, "mA")],
        "power": [(1.0, "W"), (1e-3, "mW")],
        "frequency": [(1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")],
        "temperature": [(1.0, "°C")],
        "time": [(1e-3, "ms"), (1e-6, "µs"), (1.0, "s")],
        "ratio": [(1.0, "")],
        "cost": [(1.0, "元")],
    }
    for scale, symbol in table.get(dimension, [(1.0, unit)]):
        if abs(value) >= scale or scale == table.get(dimension, [(1.0, "")])[-1][0]:
            return f"{_trim(value / scale, digits)}{symbol}"
    return f"{_trim(value, digits)}{unit}"


def _trim(x: float, digits: int) -> str:
    text = f"{x:.{digits}g}"
    return text


#: 两个量是否「同一个值」：精确匹配 / 说法不同但接近 / 明显不同。
MATCH_EXACT = "exact"
MATCH_CLOSE = "close"
MATCH_DIFFER = "differ"


def magnitudes_match(a: float, b: float, *, close_ratio: float = 0.05) -> str:
    """比较两个 SI 值。`close_ratio` 内算「说法不同」，超出即「明显不同」。"""
    if not (_finite(a) and _finite(b)):
        return MATCH_DIFFER
    if a == b:
        return MATCH_EXACT
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return MATCH_EXACT
    if abs(a - b) / scale <= 1e-9:
        return MATCH_EXACT
    if abs(a - b) / scale <= close_ratio:
        return MATCH_CLOSE
    return MATCH_DIFFER
