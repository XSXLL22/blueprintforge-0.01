"""v0.1 → v0.2 IR 迁移。

迁移**不是**验证：它只把旧的自由文本约束换算成带量纲的数值目标，并逐条
记录换算过程。两条硬规则：

1. **旧版的一句口号不能变成新版的数值承诺**。`">85%"` 变成 `0.85` 是
   一次换算，不是一次验证——迁移结果必须被重新算过才算数，所以每条换算
   都发一条 `W-IR-DECODE-002` 提醒"需人工确认"。
2. **看不懂的约束不得静默丢弃**。旧键不在 `LEGACY_CONSTRAINTS` 里、方向与
   目标语义矛盾、写法解析不出来——一律报 `E-IR-DECODE-011`，因为"被丢掉的
   约束"会让下游以为所有约束都满足了。

`migrate_ir` 是**唯一**的迁移入口，`ir.schema.from_dict` 与
`ir.decode.decode_ir` 都走它，不各写一套。
"""
from __future__ import annotations

import re

from diagnostics import ERROR, WARNING, Diagnostic
from ir.schema import TARGETS, TARGET_PLAUSIBLE
from parts.values import parse_magnitude

#: 旧约束键 → (新目标键, 「裸数字」写法时的换算系数)。
#: `ripple_target_mv` 的键名里带 mv，所以旧版裸数字 `"<50"` 就是 50mV。
LEGACY_CONSTRAINTS: dict[str, tuple[str, float]] = {
    "efficiency_target": ("efficiency_min", 1.0),
    "ripple_target_mv": ("ripple_max_v", 1e-3),
    "bom_cost_target_cny": ("bom_cost_max_cny", 1.0),
}

#: 比较方向 → 目标语义。`>`/`>=` 是下限，`<`/`<=` 是上限。
_OP_KIND = {">": "min", ">=": "min", "<": "max", "<=": "max"}

_LEADING_OP = re.compile(r"^\s*(?P<op>>=|<=|>|<|=)\s*(?P<rest>.+?)\s*$")
_BARE_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def migrate_ir(raw: dict, diags: list[Diagnostic] | None = None,
               *, source: str = "") -> tuple[dict, list[str]]:
    """把 v0.1 的 IR 字典迁移成 v0.2。

    返回 `(新字典, 转换记录)`；诊断（错误/警告）追加到 `diags`。
    非 v0.1 输入原样返回（不做任何改动）。
    """
    if not isinstance(raw, dict):
        return raw, []

    version = raw.get("schema_version")
    legacy_form = isinstance(raw.get("constraints"), dict) and "targets" not in raw
    if not (version == "0.1" or (version is None and legacy_form)):
        return raw, []

    out = dict(raw)
    notes: list[str] = []
    if version == "0.1":
        out["schema_version"] = "0.2"
        notes.append('schema_version "0.1" → "0.2"'
                     "（迁移只换算表达方式，不继承旧版任何已通过状态）")
    else:
        notes.append('缺少 schema_version，但存在 v0.1 的 constraints → '
                     '按 v0.1 迁移为 v0.2')

    constraints = out.pop("constraints", None)
    if constraints is None:
        notes.append("没有 constraints 需要迁移")
        return out, notes
    if not isinstance(constraints, dict):
        _err(diags, "E-IR-DECODE-003", "constraints",
             f"constraints 应为对象，实际 {type(constraints).__name__}")
        return out, notes

    targets = constraints_to_targets(constraints, diags)
    if targets:
        existing = out.get("targets")
        if isinstance(existing, dict):
            targets = {**targets, **existing}      # 显式 targets 优先
        out["targets"] = targets
    for key, entry in sorted(targets.items()):
        notes.append(f'constraints → targets.{key} = {entry["value"]!r}'
                     f'（原写法 {entry["source"]!r}）')
    return out, notes


def constraints_to_targets(constraints: dict,
                           diags: list[Diagnostic] | None = None
                           ) -> dict[str, dict]:
    """把旧版约束字典换算成 v0.2 的 targets 字典。

    返回 `{新键: {"value": 数值, "source": 原始写法}}`——`source` 让
    「这个数从哪来」留在 IR 里，而不是只存在于日志里。

    每条换算都发一条警告：换算是**人工复核的入口**，不是自动背书。
    """
    out: dict[str, dict] = {}
    for key, text in constraints.items():
        path = f"constraints.{key}"
        mapping = LEGACY_CONSTRAINTS.get(key)
        if mapping is None:
            _err(diags, "E-IR-DECODE-011", path,
                 f"未登记的旧版约束键 '{key}'（支持: "
                 f"{sorted(LEGACY_CONSTRAINTS)}）——不会被静默忽略，"
                 f"请改成 v0.2 的 targets 或删除")
            continue
        new_key, factor = mapping
        kind, dimension, doc = TARGETS[new_key]

        if not isinstance(text, str):
            _err(diags, "E-IR-DECODE-003", path,
                 f"旧版约束必须是字符串（如 \">85%\"），实际 "
                 f"{type(text).__name__}——不猜数值含义")
            continue

        m = _LEADING_OP.match(text)
        if m is None:
            _err(diags, "E-IR-DECODE-011", path,
                 f"约束 '{text}' 缺少比较方向（需 > / >= / < / <=）——"
                 f"没有方向的数字无法判断是上限还是下限")
            continue
        op, rest = m.group("op"), m.group("rest")
        if op == "=":
            _err(diags, "E-IR-DECODE-011", path,
                 f"约束 '{text}' 用了等号：设计目标只能是上限或下限")
            continue
        if _OP_KIND[op] != kind:
            _err(diags, "E-IR-DECODE-011", path,
                 f"约束 '{text}' 的方向与目标 {new_key}（{doc}）矛盾——"
                 f"该目标只能是{'上限' if kind == 'max' else '下限'}")
            continue

        bare = bool(_BARE_NUMBER.match(rest))
        measured = parse_magnitude(rest, dimension=dimension)
        if measured is None or measured.dimension != dimension:
            _err(diags, "E-IR-DECODE-011", path,
                 f"约束值 '{rest}' 不是合法的 {dimension} 量"
                 f"（目标 {new_key}：{doc}）")
            continue

        value = measured.value * factor if bare else measured.value
        if dimension == "ratio" and value > 1.0:
            _err(diags, "E-IR-DECODE-011", path,
                 f"约束 '{text}' 换算得 {value}，超过 1.0——"
                 f"比值的单位是分数（85% 或 0.85）")
            continue

        band = TARGET_PLAUSIBLE.get(new_key)
        if band and not (band[0] <= value <= band[1]):
            _warn(diags, "W-IR-DECODE-001", path,
                  f"{new_key}={value} 超出常见范围 {band}——请确认单位",
                  actual=str(value), expected=f"{band[0]}–{band[1]}")
        _warn(diags, "W-IR-DECODE-002", path,
              f'迁移换算："{text}" → targets.{new_key} = {value}'
              f'（{doc}）——换算是人工复核入口，不构成验证结论',
              actual=text, expected=f"{new_key} = {value}")
        out[new_key] = {"value": value, "source": text}
    return out


def _err(diags, code, path, message, **kw) -> None:
    if diags is not None:
        diags.append(Diagnostic(code, ERROR, path, message, stage="migrate", **kw))


def _warn(diags, code, path, message, **kw) -> None:
    if diags is not None:
        diags.append(Diagnostic(code, WARNING, path, message, stage="migrate", **kw))
