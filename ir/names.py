"""标识符、保留名与工程名的规则——**decode 与 validate 共用同一份实现**。

为什么单独一个模块：同一条语法规则在两个地方各写一遍，迟早会分裂成
「decode 放行、validate 拦下」。规则只有一份，调用方只决定用哪个错误码报告：

    decode.py   → E-IR-DECODE-002/009/012（输入层，早失败）
    validate.py → E-IR-NETS-003 / E-IR-STRUCT-002（语义层，防御性复核）

每个函数返回 `None` 表示合法，否则返回**人话理由**（不含错误码）。
"""
from __future__ import annotations

import re

#: 长度上限（保守值，远低于 KiCad 的限制，避免文件名/标签溢出）。
MAX_PROJECT_NAME = 64
MAX_REF = 16
MAX_NET_NAME = 64
MAX_PIN_TOKEN = 32
MAX_PORT_NAME = 32

#: Windows 保留设备名（大小写无关）。工程名会变成 `project.kicad_pro`
#: 这样的文件名，用 `con` 做工程名在 Windows 上根本创建不出文件。
WINDOWS_RESERVED = frozenset(
    [*(f"com{i}" for i in range(0, 10)), *(f"lpt{i}" for i in range(0, 10)),
     "con", "prn", "aux", "nul"])

#: 工程名：字母/下划线开头，只含字母数字下划线。
_PROJECT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 位号：1–4 个字母前缀 + 1–4 位数字 + 可选单元字母（U1 / R101 / C7 / U1A）。
_REF_RE = re.compile(r"^[A-Za-z]{1,4}[0-9]{1,4}[A-Za-z]?$")

#: 网络名 / 引脚引用：字母数字开头，允许 `_ + - .`。
#: 不允许 `/`（KiCad 里是层次路径分隔符）、空格、`#`（电源符号内部名）。
_NET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+\-.]*$")

#: 端口名：像工程名一样是标识符。
_PORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 网络保留名。`PWR_FLAG` 是 KiCad 的电源标记符号自带网络名，用户网络
#: 重名会让 ERC 的电源来源判断失去意义；`NC` 与不连接标记同名，禁止。
RESERVED_NETS = frozenset({"PWR_FLAG", "NC"})

#: 位号保留名：`#` 开头是 KiCad 给电源符号/标记用的编号空间（#PWR01、
#: #FLG0101）。IR 里出现 `#` 开头的位号意味着有人想手工干预这一层。
RESERVED_REF_PREFIX = "#"

#: 位号保留名（与生成器自动创建的符号撞名）。
RESERVED_REFS = frozenset({"PWR_FLAG"})


def project_name_error(name: object) -> str | None:
    """工程名合法性。返回 None 表示合法。"""
    if not isinstance(name, str):
        return f"必须是字符串，实际 {type(name).__name__}"
    if not name:
        return "不能为空"
    if len(name) > MAX_PROJECT_NAME:
        return f"过长（{len(name)} > {MAX_PROJECT_NAME}）"
    if name != name.strip():
        return "首尾不能有空白"
    if name.endswith("."):
        return "不能以点结尾（Windows 会截断文件名）"
    if name.lower() in WINDOWS_RESERVED:
        return f"'{name}' 是 Windows 保留设备名，无法作为文件名"
    if not _PROJECT_RE.match(name):
        return "只允许字母/数字/下划线，且必须以字母或下划线开头"
    return None


def ref_error(ref: object) -> str | None:
    """位号合法性。"""
    if not isinstance(ref, str):
        return f"必须是字符串，实际 {type(ref).__name__}"
    if not ref:
        return "不能为空"
    if len(ref) > MAX_REF:
        return f"过长（{len(ref)} > {MAX_REF}）"
    if ref.startswith(RESERVED_REF_PREFIX):
        return f"'{RESERVED_REF_PREFIX}' 开头是 KiCad 电源符号的保留编号空间"
    if ref.upper() in RESERVED_REFS:
        return f"'{ref}' 与生成器自动创建的符号撞名"
    if not _REF_RE.match(ref):
        return "应为 1–4 个字母 + 1–4 位数字（可带单元字母），如 U1 / R101"
    return None


def net_name_error(name: object) -> str | None:
    """网络名合法性。"""
    if not isinstance(name, str):
        return f"必须是字符串，实际 {type(name).__name__}"
    if not name:
        return "不能为空"
    if len(name) > MAX_NET_NAME:
        return f"过长（{len(name)} > {MAX_NET_NAME}）"
    if name.upper() in RESERVED_NETS:
        return f"'{name}' 是保留网络名（KiCad 电源标记/不连接标记）"
    if not _NET_RE.match(name):
        return ("只允许字母/数字/下划线/加号/减号/点，且以字母或数字开头"
                "（'/' 是层次路径分隔符，不允许）")
    return None


def pin_token_error(token: object) -> str | None:
    """引脚引用（脚号或功能名）合法性。"""
    if not isinstance(token, str):
        return f"必须是字符串，实际 {type(token).__name__}"
    if not token:
        return "不能为空"
    if len(token) > MAX_PIN_TOKEN:
        return f"过长（{len(token)} > {MAX_PIN_TOKEN}）"
    if not _NET_RE.match(token):
        return ("只允许字母/数字/下划线/加号/减号/点，且以字母或数字开头"
                "（不支持空格与 '~'，请写数据手册脚名或物理脚号）")
    return None


def port_name_error(name: object) -> str | None:
    """外部端口名合法性。"""
    if not isinstance(name, str):
        return f"必须是字符串，实际 {type(name).__name__}"
    if not name:
        return "不能为空"
    if len(name) > MAX_PORT_NAME:
        return f"过长（{len(name)} > {MAX_PORT_NAME}）"
    if not _PORT_RE.match(name):
        return "只允许字母/数字/下划线，且必须以字母或下划线开头"
    return None
