"""IR 严格解码：只回答「这棵 JSON 树能不能构成一个 v0.2 的 IR」。

分工（**不要**在这里做电路语义判断）：

    decode.py   —— 结构、类型、语法。不通过就绝不进入语义检查。
    validate.py —— 这个 IR 作为电路设计是否成立（范围、拓扑、连接、器件）。

为什么结构必须先行（§4.4）：NaN 参与的所有比较都是 False，布尔值会被
当成 1/0，重复 key 取哪个值取决于解析器，拼错的约束键会被静默忽略——
这些输入会让后续每一项检查**失去意义**，却仍然一路"通过"到生成工程文件。
所以本模块的规则是：**任何 error ⇒ 没有 draft**，调用方不得继续。

**同一个缺陷只用一个错误码**：语义层（validate.py）若已有同义码，
结构层复用它，而不是新造一个——否则同一个错误会因为入口不同而给出
不同的码，使用者的修复流程就得记两套。复用关系：

    project      → E-IR-STRUCT-002      module_type → E-IR-STRUCT-003
    topology     → E-IR-STRUCT-004      role        → E-IR-PARTS-002
    网络名        → E-IR-NETS-003       其余标识符   → E-IR-DECODE-009

不抛异常的输入路径（都被转成结构化诊断，绝不泄漏 AttributeError/TypeError）：

| 输入 | 错误码 |
|---|---|
| 顶层不是对象 | E-IR-DECODE-001 |
| JSON 文本不可解析 / 数值字面量异常 | E-IR-DECODE-010 |
| 必需字段缺失 | E-IR-DECODE-002 |
| 类型错误（bool 冒充数值、字符串冒充数值…） | E-IR-DECODE-003 |
| NaN / Infinity / 1e400 溢出 | E-IR-DECODE-004 |
| 未知字段（严格模式拒绝） | E-IR-DECODE-005 |
| 重复 JSON key | E-IR-DECODE-006 |
| 版本不支持且无迁移路径 | E-IR-DECODE-007 |
| 空数组 / 元素不是对象 | E-IR-DECODE-008 |
| 非法位号 / 网络名 / 引脚引用 / 端口名 | E-IR-DECODE-009 |
| 保留名冲突（`#` 开头位号、PWR_FLAG/NC 网络） | E-IR-DECODE-012 |
| 枚举取值不在允许集合 | E-IR-DECODE-013 |
| 目标键未登记 / 单位与量纲不符 | E-IR-DECODE-011 |
| 数值疑似量纲错误（超出常理范围） | W-IR-DECODE-001（警告） |
| v0.1 迁移换算 | W-IR-DECODE-002（警告） |
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from diagnostics import ERROR, WARNING, Diagnostic
from ir import names
from ir.migrate import migrate_ir
from ir.schema import (COMPONENT_KEYS, ELECTRICAL_KEYS, MODULE_TYPES,
                       NETCLASSES, NET_KEYS, PIN_KEYS, PORT_DIRECTIONS,
                       PORT_KEYS, ROLES, SCHEMA_VERSION, SUPPORTED_VERSIONS,
                       TARGET_PLAUSIBLE, TARGETS, TOP_LEVEL_KEYS, TOPOLOGIES,
                       Component, Electrical, Net, PinRef, Port, PowerIR,
                       Range, Target)


class DecodeResult:
    """解码结论。

    `draft is None` ⇔ 存在结构错误 ⇔ 调用方**必须停止**（不得生成工程文件）。
    有语义错误（值不合理）但结构合法时，`draft` 仍然存在，交给 validate 报。
    """
    __slots__ = ("draft", "diagnostics", "migrations", "source")

    def __init__(self, draft: PowerIR | None,
                 diagnostics: tuple[Diagnostic, ...] = (),
                 migrations: tuple[str, ...] = (), source: str = "") -> None:
        self.draft = draft
        self.diagnostics = diagnostics
        self.migrations = migrations
        self.source = source

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.is_error)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if not d.is_error)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def usable(self) -> bool:
        """可以继续往下走吗——只有结构合格、且确实拿到了 IR 才行。"""
        return self.draft is not None

    def render(self) -> str:
        lines = [d.render() for d in self.diagnostics]
        if self.migrations:
            lines.append("迁移记录：")
            lines.extend(f"  - {m}" for m in self.migrations)
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (f"DecodeResult(usable={self.usable}, "
                f"errors={len(self.errors)}, warnings={len(self.warnings)})")


# ---- JSON 层：保留字面量与重复键 ---------------------------------------------

class _Num(float):
    """带原始字面量的数值。

    只为把 `1e400`（溢出成 inf）与 `NaN`/`Infinity` 区分开来——
    错误消息要能指出**输入里写的是什么**，否则用户不知道改哪里。
    """
    literal: str

    def __new__(cls, literal: str) -> "_Num":
        obj = super().__new__(cls, literal)
        obj.literal = literal
        return obj


class _Obj(dict):
    """记录重复键的对象。JSON 里同一个键出现多次时，谁生效取决于解析器。"""
    dups: tuple[str, ...]


def _object_pairs(pairs: list[tuple[str, object]]) -> _Obj:
    obj = _Obj()
    dup: list[str] = []
    for key, value in pairs:
        if key in obj:
            dup.append(key)
        obj[key] = value            # 保留最后一个（Python 的常规语义）
    obj.dups = tuple(dict.fromkeys(dup))
    return obj


#: JSON 值类型 → 人话名称（错误消息里用）。
def _typename(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# ---- 解码上下文 --------------------------------------------------------------

class _Ctx:
    """收集诊断的小工具。所有检查都**先报错再返回 None**，不抛异常。"""

    def __init__(self, source: str = "") -> None:
        self.diags: list[Diagnostic] = []
        self.source = source

    # -- 诊断 --
    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diags if d.is_error)

    def err(self, code: str, path: str, message: str, **kw) -> None:
        self.diags.append(Diagnostic(code, ERROR, path, message,
                                     stage="decode", **kw))

    def warn(self, code: str, path: str, message: str, **kw) -> None:
        self.diags.append(Diagnostic(code, WARNING, path, message,
                                     stage="decode", **kw))

    # -- 基本类型 --
    def obj(self, value: object, path: str) -> dict | None:
        if isinstance(value, _Obj) and value.dups:
            for key in value.dups:
                self.err("E-IR-DECODE-006", path,
                         f"重复的 JSON key '{key}'——同一个键写了多次，"
                         f"取哪个值取决于解析器，必须消除歧义",
                         object_id=key, actual=key)
        if not isinstance(value, dict):
            self.err("E-IR-DECODE-003", path,
                     f"应为对象，实际 {_typename(value)}",
                     actual=_typename(value), expected="object")
            return None
        return value

    def arr(self, value: object, path: str, *,
            non_empty: bool = False) -> list | None:
        if not isinstance(value, list):
            self.err("E-IR-DECODE-003", path,
                     f"应为数组，实际 {_typename(value)}",
                     actual=_typename(value), expected="array")
            return None
        if non_empty and not value:
            self.err("E-IR-DECODE-008", path,
                     "数组为空——没有任何可生成的内容（空表不等于「没有约束」）",
                     actual="[]", expected="非空数组")
            return None
        return value

    def keys(self, obj: dict, path: str, *, allowed: tuple[str, ...],
             required: tuple[str, ...] = ()) -> tuple[str, ...]:
        """检查未知字段与缺失字段，返回缺失的必需字段。"""
        for key in obj:
            if key not in allowed:
                self.err("E-IR-DECODE-005", f"{path}.{key}" if path else key,
                         f"未知字段 '{key}'——拼写错误或版本不匹配；"
                         f"确需扩展请放进 x_ext（允许: {', '.join(allowed)}）",
                         object_id=key, actual=key,
                         expected=", ".join(allowed))
        missing = tuple(k for k in required if k not in obj)
        for key in missing:
            self.err("E-IR-DECODE-002", f"{path}.{key}" if path else key,
                     f"缺少必需字段 '{key}'", object_id=key, expected=key)
        return missing

    def text(self, value: object, path: str, *, allow_empty: bool = False,
             optional: bool = False, hint: str = "") -> str | None:
        if value is None and optional:
            return None
        if not isinstance(value, str):
            extra = f"；{hint}" if hint else ""
            self.err("E-IR-DECODE-003", path,
                     f"应为字符串，实际 {_typename(value)}{extra}",
                     actual=_typename(value), expected="string")
            return None
        if not allow_empty and not value.strip():
            self.err("E-IR-DECODE-003", path, "字符串为空", actual="''",
                     expected="非空")
            return None
        return value

    def num(self, value: object, path: str, *, hint: str = "") -> float | None:
        if isinstance(value, bool):
            self.err("E-IR-DECODE-003", path,
                     "布尔值不能冒充数值（true/false 会被当成 1/0）",
                     actual=str(value).lower(), expected="number")
            return None
        if not isinstance(value, (int, float)):
            extra = f"；{hint}" if hint else ""
            self.err("E-IR-DECODE-003", path,
                     f"应为数值，实际 {_typename(value)}{extra}",
                     actual=_typename(value), expected="number")
            return None
        number = float(value)
        if not math.isfinite(number):
            literal = getattr(value, "literal", None) or (
                "NaN" if math.isnan(number) else "Infinity")
            self.err("E-IR-DECODE-004", path,
                     f"数值非有限（{literal}）——NaN/Infinity 会让所有比较"
                     f"结果为假，等价于「没有违规」，必须写成有限数",
                     actual=literal, expected="有限数值")
            return None
        return number

    def flag(self, value: object, path: str) -> bool | None:
        if not isinstance(value, bool):
            self.err("E-IR-DECODE-003", path,
                     f"应为 true/false，实际 {_typename(value)}",
                     actual=_typename(value), expected="boolean")
            return None
        return value

    def ident(self, value: object, path: str, rule, *, kind: str,
              hint: str = "", code: str = "E-IR-DECODE-009") -> str | None:
        if not isinstance(value, str):
            self.err("E-IR-DECODE-003", path,
                     f"{kind}应为字符串，实际 {_typename(value)}",
                     actual=_typename(value), expected="string")
            return None
        reason = rule(value)
        if reason is not None:
            extra = f"；{hint}" if hint else ""
            self.err(code, path, f"{kind} '{value}' 非法：{reason}{extra}",
                     object_id=value, actual=value)
            return None
        return value

    def enum(self, value: object, path: str, allowed: tuple[str, ...],
             *, kind: str, code: str = "E-IR-DECODE-013") -> str | None:
        if not isinstance(value, str):
            self.err("E-IR-DECODE-003", path,
                     f"{kind}应为字符串，实际 {_typename(value)}",
                     actual=_typename(value), expected="string")
            return None
        if value not in allowed:
            self.err(code, path,
                     f"{kind} '{value}' 不在允许集合中"
                     f"（允许: {', '.join(allowed)}）",
                     actual=value, expected=", ".join(allowed))
            return None
        return value

    def text_list(self, value: object, path: str) -> tuple[str, ...] | None:
        if value is None:
            return ()
        items = self.arr(value, path)
        if items is None:
            return None
        out: list[str] = []
        for i, item in enumerate(items):
            text = self.text(item, f"{path}[{i}]", allow_empty=True)
            if text is not None:
                out.append(text)
        return tuple(out)


# ---- 顶层入口 ---------------------------------------------------------------

def decode_text(text: str, *, source: str = "",
                migrate: bool = True) -> DecodeResult:
    """解析 JSON 文本并严格解码。"""
    if not isinstance(text, str):
        ctx = _Ctx(source)
        ctx.err("E-IR-DECODE-001", "",
                f"输入不是文本，实际 {_typename(text)}",
                actual=_typename(text), expected="JSON 文本")
        return DecodeResult(None, tuple(ctx.diags), (), source)

    try:
        raw = json.loads(text, parse_float=_Num, parse_constant=_Num,
                         object_pairs_hook=_object_pairs)
    except json.JSONDecodeError as exc:
        diag = Diagnostic(
            "E-IR-DECODE-010", ERROR, "",
            f"JSON 无法解析：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
            stage="decode", evidence=source)
        return DecodeResult(None, (diag,), (), source)
    except (ValueError, OverflowError) as exc:
        diag = Diagnostic(
            "E-IR-DECODE-010", ERROR, "",
            f"JSON 里的数值无法解析：{exc}", stage="decode", evidence=source)
        return DecodeResult(None, (diag,), (), source)

    return decode_ir(raw, source=source, migrate=migrate)


def decode_ir(raw: object, *, source: str = "",
              migrate: bool = True) -> DecodeResult:
    """严格解码一个已解析的 JSON 树。"""
    ctx = _Ctx(source)
    notes: tuple[str, ...] = ()

    if migrate:
        raw, note_list = migrate_ir(raw, ctx.diags, source=source)
        notes = tuple(note_list)

    draft = None
    if isinstance(raw, dict) and not ctx.errors:
        draft = _build(raw, ctx)
    elif not isinstance(raw, dict):
        ctx.err("E-IR-DECODE-001", "",
                f"IR 顶层必须是对象，实际 {_typename(raw)}",
                actual=_typename(raw), expected="object")

    if ctx.errors:
        draft = None                     # 结构不合格 ⇒ 没有可用的 IR
    return DecodeResult(draft, tuple(ctx.diags), notes, source)


# ---- 逐字段构建 --------------------------------------------------------------

def _build(raw: dict, ctx: _Ctx) -> PowerIR | None:
    top = ctx.obj(raw, "")
    if top is None:
        return None
    missing = ctx.keys(top, "", allowed=TOP_LEVEL_KEYS, required=(
        "schema_version", "project", "module_type", "topology",
        "electrical", "components", "nets"))
    if missing:
        return None                      # 核心字段缺失，继续解码只会产生噪音

    version = ctx.text(top["schema_version"], "schema_version")
    if version is not None and version not in SUPPORTED_VERSIONS:
        ctx.err("E-IR-DECODE-007", "schema_version",
                f"schema_version '{version}' 不受支持（支持 "
                f"{', '.join(SUPPORTED_VERSIONS)}；当前 {SCHEMA_VERSION}）"
                f"——本版本没有到它的迁移路径，拒绝猜测字段含义",
                actual=version, expected=", ".join(SUPPORTED_VERSIONS))

    project = ctx.ident(top["project"], "project", names.project_name_error,
                        kind="工程名", code="E-IR-STRUCT-002")
    module_type = ctx.enum(top["module_type"], "module_type", MODULE_TYPES,
                           kind="module_type", code="E-IR-STRUCT-003")
    topology = None
    if module_type is not None:
        topology = ctx.enum(top["topology"], "topology",
                            TOPOLOGIES[module_type], kind="topology",
                            code="E-IR-STRUCT-004")

    electrical = _electrical(top["electrical"], ctx)
    components = _components(top["components"], ctx)
    nets = _nets(top["nets"], ctx)
    targets = _targets(top.get("targets"), ctx)
    ports = _ports(top.get("ports"), ctx)
    rationale = ctx.text(top.get("design_rationale", ""), "design_rationale",
                         allow_empty=True, optional=True) or ""
    assumptions = ctx.text_list(top.get("assumptions"), "assumptions") or ()
    risks = ctx.text_list(top.get("risks"), "risks") or ()

    if ctx.errors:
        return None
    assert (version and project and module_type and topology
            and electrical and components is not None and nets is not None
            and targets is not None and ports is not None)
    return PowerIR(
        schema_version=version, project=project, module_type=module_type,
        topology=topology, electrical=electrical, components=components,
        nets=nets, targets=targets, ports=ports, design_rationale=rationale,
        assumptions=assumptions, risks=risks, raw=raw,
    )


def _electrical(value: object, ctx: _Ctx) -> Electrical | None:
    obj = ctx.obj(value, "electrical")
    if obj is None:
        return None
    if ctx.keys(obj, "electrical", allowed=ELECTRICAL_KEYS,
                required=("vin", "vout", "iout_max")):
        return None
    vin = _range(obj["vin"], "electrical.vin", ctx)
    vout = _range(obj["vout"], "electrical.vout", ctx)
    iout = ctx.num(obj["iout_max"], "electrical.iout_max")
    if vin is None or vout is None or iout is None:
        return None
    return Electrical(vin=vin, vout=vout, iout_max=iout)


def _range(value: object, path: str, ctx: _Ctx) -> Range | None:
    obj = ctx.obj(value, path)
    if obj is None:
        return None
    if ctx.keys(obj, path, allowed=("min", "typ", "max", "x_ext"),
                required=("min", "typ", "max")):
        return None
    values = [ctx.num(obj[k], f"{path}.{k}") for k in ("min", "typ", "max")]
    if any(v is None for v in values):
        return None
    return Range(min=values[0], typ=values[1], max=values[2])   # type: ignore[arg-type]


def _components(value: object, ctx: _Ctx,
                path_prefix: str = "components") -> tuple[Component, ...] | None:
    items = ctx.arr(value, path_prefix, non_empty=True)
    if items is None:
        return None
    out: list[Component] = []
    for i, item in enumerate(items):
        base = f"{path_prefix}[{i}]"
        obj = ctx.obj(item, base)
        if obj is None:
            continue
        if ctx.keys(obj, base, allowed=COMPONENT_KEYS, required=("ref", "part")):
            continue
        ref = _ref(ctx, obj["ref"], f"{base}.ref")
        part = ctx.text(obj["part"], f"{base}.part",
                        hint="器件型号必须是器件库里的名字")
        role = ctx.enum(obj.get("role", "passive"), f"{base}.role", ROLES,
                        kind="role", code="E-IR-PARTS-002")
        text_fields = {}
        for key in ("value", "footprint", "rating"):
            hint = ("阻容感参数写成带单位的字符串，如 '10uH' / '0.1uF' / '49.9k'"
                    if key == "value" else "")
            text_fields[key] = ctx.text(obj.get(key), f"{base}.{key}",
                                        allow_empty=True, optional=True,
                                        hint=hint)
        nc = _pin_token_list(obj.get("nc"), f"{base}.nc", ctx)
        if ref is None or part is None or role is None or nc is None:
            continue
        out.append(Component(ref=ref, part=part, role=role,
                             value=text_fields["value"],
                             footprint=text_fields["footprint"],
                             rating=text_fields["rating"], nc=nc))
    return tuple(out)


def _nets(value: object, ctx: _Ctx) -> tuple[Net, ...] | None:
    items = ctx.arr(value, "nets", non_empty=True)
    if items is None:
        return None
    out: list[Net] = []
    for i, item in enumerate(items):
        base = f"nets[{i}]"
        obj = ctx.obj(item, base)
        if obj is None:
            continue
        if ctx.keys(obj, base, allowed=NET_KEYS, required=("name", "pins")):
            continue
        name = _net_name(ctx, obj["name"], f"{base}.name")
        pins_raw = ctx.arr(obj["pins"], f"{base}.pins")
        pins: list[PinRef] = []
        if pins_raw is not None:
            for j, p in enumerate(pins_raw):
                pbase = f"{base}.pins[{j}]"
                pobj = ctx.obj(p, pbase)
                if pobj is None:
                    continue
                if ctx.keys(pobj, pbase, allowed=PIN_KEYS, required=("ref", "pin")):
                    continue
                pref = ctx.ident(pobj["ref"], f"{pbase}.ref", names.ref_error,
                                 kind="位号")
                ptok = ctx.ident(pobj["pin"], f"{pbase}.pin",
                                 names.pin_token_error, kind="引脚引用",
                                 hint="写数据手册脚名（VIN）或物理脚号（7）")
                if pref is not None and ptok is not None:
                    pins.append(PinRef(ref=pref, pin=ptok))
        netclass = None
        if obj.get("netclass") is not None:
            netclass = ctx.enum(obj["netclass"], f"{base}.netclass",
                                NETCLASSES, kind="netclass")
        if name is not None:
            out.append(Net(name=name, pins=tuple(pins), netclass=netclass))
    return tuple(out)


def _targets(value: object, ctx: _Ctx) -> tuple[Target, ...] | None:
    if value is None:
        return ()
    obj = ctx.obj(value, "targets")
    if obj is None:
        return None
    out: list[Target] = []
    for key, raw in obj.items():
        path = f"targets.{key}"
        entry = raw
        if key not in TARGETS:
            ctx.err("E-IR-DECODE-011", path,
                    f"未登记的设计目标键 '{key}'——拼错的约束不会被静默忽略"
                    f"（支持: {', '.join(sorted(TARGETS))}）",
                    object_id=key, actual=key, expected=", ".join(sorted(TARGETS)))
            continue
        kind, dimension, doc = TARGETS[key]
        conditions: dict = {}
        source = ""
        if isinstance(raw, dict):
            if ctx.keys(raw, path, allowed=("value", "conditions", "source", "x_ext"),
                        required=("value",)):
                continue
            entry = raw["value"]
            source = ctx.text(raw.get("source", ""), f"{path}.source",
                              allow_empty=True, optional=True) or ""
            conditions = _conditions(raw.get("conditions"), f"{path}.conditions", ctx)
        number = ctx.num(entry, path,
                         hint=f"目标值必须是数值 + 键名带单位（{doc}）；"
                              f"如 {key} = 0.85，不要写 '85%'")
        if number is None:
            continue
        if dimension == "ratio" and number > 1.0:
            ctx.err("E-IR-DECODE-011", path,
                    f"{key} 的取值是**分数**，{number} 已超过 1.0"
                    f"（{doc}）——若想写百分比请用 0.85",
                    actual=str(number), expected="≤ 1.0")
            continue
        band = TARGET_PLAUSIBLE.get(key)
        if band is not None and not (band[0] <= number <= band[1]):
            ctx.warn("W-IR-DECODE-001", path,
                     f"{key} = {number} 超出常见范围 {band}——请确认单位/量纲"
                     f"（mV 与 V 差 1000 倍，% 与分数差 100 倍）",
                     actual=str(number), expected=f"{band[0]}–{band[1]}")
        out.append(Target(key=key, kind=kind, value=number, dimension=dimension,
                          source=source, conditions=conditions))
    return tuple(out)


def _conditions(value: object, path: str, ctx: _Ctx) -> dict:
    """验证条件（测量带宽、负载阶跃、温度点…）。键值都必须是标量。"""
    if value is None:
        return {}
    obj = ctx.obj(value, path)
    if obj is None:
        return {}
    out: dict = {}
    for key, item in obj.items():
        if isinstance(item, str):
            out[key] = item
        elif isinstance(item, bool) or not isinstance(item, (int, float)):
            ctx.err("E-IR-DECODE-003", f"{path}.{key}",
                    f"验证条件只能是字符串或有限数值，实际 {_typename(item)}",
                    actual=_typename(item), expected="string 或 number")
        else:
            number = ctx.num(item, f"{path}.{key}")
            if number is not None:
                out[key] = number
    return out


def _ports(value: object, ctx: _Ctx) -> tuple[Port, ...] | None:
    if value is None:
        return ()
    items = ctx.arr(value, "ports")
    if items is None:
        return None
    out: list[Port] = []
    for i, item in enumerate(items):
        base = f"ports[{i}]"
        obj = ctx.obj(item, base)
        if obj is None:
            continue
        if ctx.keys(obj, base, allowed=PORT_KEYS, required=("name", "net")):
            continue
        name = ctx.ident(obj["name"], f"{base}.name", names.port_name_error,
                         kind="端口名")
        net = _net_name(ctx, obj["net"], f"{base}.net")
        direction = ctx.enum(obj.get("direction", "input"),
                             f"{base}.direction", PORT_DIRECTIONS,
                             kind="端口方向")
        drives = ctx.flag(obj.get("drives", False), f"{base}.drives")
        note = ctx.text(obj.get("note", ""), f"{base}.note", allow_empty=True,
                        optional=True) or ""
        if None in (name, net, direction, drives):
            continue
        out.append(Port(name=name, direction=direction, net=net,
                        drives=bool(drives), note=note))
    return tuple(out)


# ---- 标识符（保留名单独给码） -------------------------------------------------

def _ref(ctx: _Ctx, value: object, path: str) -> str | None:
    if isinstance(value, str) and _is_reserved_ref(value):
        ctx.err("E-IR-DECODE-012", path,
                f"位号 '{value}' 使用了保留名——'#' 开头是 KiCad 电源符号/"
                f"标记的编号空间（#PWR01、#FLG0101），IR 不得占用",
                object_id=value, actual=value)
        return None
    return ctx.ident(value, path, names.ref_error, kind="位号")


def _net_name(ctx: _Ctx, value: object, path: str) -> str | None:
    if isinstance(value, str) and value.upper() in names.RESERVED_NETS:
        ctx.err("E-IR-DECODE-012", path,
                f"网络名 '{value}' 是保留名（KiCad 电源标记/不连接标记同名）",
                object_id=value, actual=value)
        return None
    return ctx.ident(value, path, names.net_name_error, kind="网络名",
                     code="E-IR-NETS-003")


def _is_reserved_ref(ref: str) -> bool:
    return (ref.startswith(names.RESERVED_REF_PREFIX)
            or ref.upper() in names.RESERVED_REFS)


def _pin_token_list(value: object, path: str, ctx: _Ctx) -> tuple[str, ...] | None:
    if value is None:
        return ()
    items = ctx.arr(value, path)
    if items is None:
        return None
    out: list[str] = []
    for i, item in enumerate(items):
        token = ctx.ident(item, f"{path}[{i}]", names.pin_token_error,
                          kind="引脚引用")
        if token is not None:
            out.append(token)
    if len(set(out)) != len(out):
        ctx.err("E-IR-DECODE-008", path,
                f"nc 列表里有重复项: {sorted({t for t in out if out.count(t) > 1})}",
                actual=str(out))
        return None
    return tuple(out)
