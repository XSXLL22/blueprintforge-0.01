"""KiCad S-expression 词法/语法解析。

KiCad 的 .kicad_mod / .kicad_sch / 网表（kicadsexpr）都是 s-expr。这里只做
**通用且正确**的解析：引号、反斜杠转义、字符串内括号、任意嵌套、UTF-8。
不做「数括号」或按行正则——那类实现遇到字符串里的括号就会错位。

返回结构：
- 列表  → 子表达式
- `Atom`（str 子类）   → 未加引号的原子，例如 `pad`、`0.5`
- `SString`（str 子类）→ 带引号的字符串字面量，例如 `"R_0603_1608Metric"`

两者都是 `str` 子类，所以 `node[0] == "pad"` 这类比较可以直接写。
"""
from __future__ import annotations

__all__ = ["Atom", "SString", "SExprError", "parse", "parse_one",
           "children", "child", "values_of", "atom_text"]

_WS = " \t\r\n\f\v"


class Atom(str):
    """未加引号的 s-expr 原子。"""


class SString(str):
    """加引号的 s-expr 字符串字面量（转义已解开）。"""


class SExprError(ValueError):
    """语法错误。带行列号，便于定位到源文件。"""

    def __init__(self, message: str, line: int, column: int):
        super().__init__(f"{message}（第 {line} 行第 {column} 列）")
        self.line = line
        self.column = column


def _advance(text: str, i: int, ch: str) -> int:
    return i + 1


def parse(text: str) -> list:
    """解析整段文本，返回顶层表达式列表。

    文本里出现多个顶层表达式是合法的（Kicad 有时这样写）；
    若只需要一个，用 `parse_one`。
    """
    root: list = []
    stack: list[list] = [root]
    i, n = 0, len(text)
    line, col = 1, 1

    def bump(pos: int) -> None:
        nonlocal line, col
        for ch in text[pos:i]:
            if ch == "\n":
                line, col = line + 1, 1
            else:
                col += 1

    while i < n:
        ch = text[i]
        if ch in _WS:
            i += 1
            if ch == "\n":
                line, col = line + 1, 1
            else:
                col += 1
            continue
        if ch == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
            i += 1
            col += 1
            continue
        if ch == ")":
            if len(stack) == 1:
                raise SExprError("多余的右括号", line, col)
            stack.pop()
            i += 1
            col += 1
            continue
        if ch == '"':
            start, start_line, start_col = i, line, col
            i += 1
            col += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise SExprError("字符串未闭合", start_line, start_col)
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise SExprError("转义符后文本结束", line, col)
                    nxt = text[i + 1]
                    # KiCad 写文件时用 \" \\ \n \t \r；其余按字面保留
                    buf.append({"n": "\n", "t": "\t", "r": "\r",
                                '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                    i += 2
                    col += 2
                    continue
                if c == '"':
                    i += 1
                    col += 1
                    break
                buf.append(c)
                i += 1
                col += 1
            stack[-1].append(SString("".join(buf)))
            continue
        # 普通原子：到空白、括号或引号为止
        start = i
        while i < n and text[i] not in _WS and text[i] not in '()"':
            i += 1
        col += i - start
        stack[-1].append(Atom(text[start:i]))

    if len(stack) != 1:
        raise SExprError("括号未闭合", line, col)
    return root


def parse_one(text: str) -> list:
    """解析恰好一个顶层表达式，多于一个或有剩余内容都报错。"""
    nodes = parse(text)
    if len(nodes) != 1:
        raise SExprError(f"期望 1 个顶层表达式，实际 {len(nodes)} 个", 1, 1)
    return nodes[0]


# ---- 访问器（都用不抛异常的形式，缺失返回空） --------------------------------

def children(node, key: str) -> list[list]:
    """所有形如 `(key ...)` 的直接子表达式。"""
    if not isinstance(node, list):
        return []
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def child(node, key: str) -> list | None:
    """第一个形如 `(key ...)` 的直接子表达式。"""
    got = children(node, key)
    return got[0] if got else None


def values_of(node, key: str) -> list[str]:
    """取 `(key a b c)` 里 key 之后的原子/字符串参数。"""
    got = child(node, key)
    return list(got[1:]) if got else []


def atom_text(value) -> str:
    """把原子/字符串还原为文本（给错误消息用）。"""
    if isinstance(value, str):
        return str(value)
    if isinstance(value, list):
        return "(" + " ".join(atom_text(v) for v in value) + ")"
    return str(value)
