"""器件目录快照 —— 校验与生成共用的**唯一**目录来源。

三个原则（对应实施指南 4.2）：

1. **快照而不是全局表**。所有 helper 收同一个 `CatalogSnapshot`，避免
   「目录注入没贯穿」那类缺陷：注入的目录必须处处生效，否则就当作没注入。
2. **电气主数据与商业报价分开哈希**。`revision` 只覆盖电气/工艺事实
   （脚位、封装、额定值、来源、子图规则），报价/库存进 `supply_revision`。
   否则一次价格刷新会让所有已验证的 IR 变成「目录变了」。
3. **自校验要读实际封装文件**。按 `CatalogValidationPolicy.pad_provider`
   注入的封装读取器核对「符号脚号 ↔ 封装电气焊盘号」；核对不了时由策略
   决定是阻断（`require_pads=True`）还是记未核对（见下）。

**没有"隐式"的第三态**：核对不了既不是错误也不是通过。此前 `_check_pads`
把读取器的所有异常吞成 `W-CAT-001`，于是「没核对」只能从 `ok=True` 里被读成
「没问题」，测试想在 `validate()` 外面捕获 `FootprintError` 来跳过也永远捕不到
（复核报告 P1#1）。现在由 `CatalogValidationPolicy` 显式声明，未核对项进
`Outcome.unverified`。

本模块只依赖 parts/ 与 diagnostics.py（底层），不 import backend。
封装读取器由调用方注入（生产用 backend.kicad.footprint.electrical_pads）——
这也正是"策略"必须由调用方给出的原因：本层无法知道这台机器上有没有 KiCad。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from diagnostics import (
    CODES, Diagnostic, Outcome,
)
from parts.partsdb import (CATEGORIES, PARTS, REVIEW_STATES, Part)

#: 封装读取器：给定 "库:名" 返回该封装的电气焊盘序列。
#: 元素需要提供 `.number`（焊盘号）与 `.describe()`。
PadProvider = Callable[[str], tuple]

#: 生命周期里允许进入生成阶段的取值。
_USABLE_LIFECYCLE = ("active", "nrfnd")

#: 电气主数据字段（进 revision 哈希）。
_ELECTRICAL_FIELDS = (
    "name", "category", "package", "lifecycle", "lib_id", "footprint",
    "allowed_footprints", "pins", "nominal", "ratings", "operating",
    "abs_max", "supported_topologies", "sources", "review_state",
    "manufacturer", "mpn",
)

#: 商业字段（进 supply_revision 哈希，与电气主数据解耦）。
_SUPPLY_FIELDS = ("price_cny", "stock", "lcsc", "alternatives")


@dataclass(frozen=True)
class CatalogValidationPolicy:
    """本次要核对到什么程度——**由调用方（profile）显式声明**。

    存在的理由：目录自校验里唯一与环境有关的一步是"能不能读封装库"，而
    `parts/` 这一层不允许 import backend，所以它只能由调用方注入。此前这
    件事是靠 `_check_pads` 里 `except Exception` 的隐式结果表达的（一律降级
    成 warning），于是"核对不了"与"核对通过"在返回值上无法区分，而"这次
    到底算不算已核对"取决于异常类型——不是任何人明确决定的。

    - `pad_provider=None`：没有封装读取器（本机没有 KiCad 封装库）。
    - `require_pads=False`（generate 口径）：核对不了记 `W-CAT-001`，同时
      进 `Outcome.unverified`——**未核对不是通过**，但不阻断"仅生成"。
    - `require_pads=True`（schematic 口径）：目录里带封装的记录必须真核对；
      没有读取器、或读取失败，都是阻断错误 `E-CAT-006`。
      **核对不到就不能声称做了原理图级核对**，这是这条策略的全部意义。

    注意策略只管"核对不了"怎么办：真核出来不符（`E-CAT-002`）在任何口径下
    都是错误——脚号对不上焊盘就是电路错了，与在哪个 profile 里无关。
    """
    pad_provider: PadProvider | None = None
    require_pads: bool = False


def _pin_dict(pin) -> dict:
    return {"number": pin.number, "name": pin.name, "kind": pin.kind,
            "required": pin.required, "nc_condition": pin.nc_condition}


def _source_dict(src) -> dict:
    # `note` 说明这条资料**支撑了哪些字段**（T00 起就是 sourced 记录的必填项）。
    # 它不在哈希里时，改掉"这一页支撑什么"不会改变 revision——两份证据含义
    # 不同的目录会被当成同一版（复核报告 P1#2）。
    return {"kind": src.kind, "url": src.url, "revision": src.revision,
            "pages": src.pages, "retrieved": src.retrieved, "note": src.note}


def _rule_dict(rule) -> dict:
    """一条连接规则的稳定序列化：类型名 + **全部**字段（含页码依据）。

    规则是数据手册要求的外围子图，`checks/subgraph.py` 按它判错。规则内容
    变了就是电气要求变了，必须改变 revision；`evidence` 也进哈希，因为判错
    时要指给人看的就是它。

    不是数据类就报错（而不是 `str()` 一下了事）：那样等于把一个来路不明的
    对象悄悄哈希成常量，改了它 revision 不动——正是要防的那类静默。
    """
    if isinstance(rule, type) or not dataclasses.is_dataclass(rule):
        raise TypeError(
            f"连接规则必须是数据类**实例**，得到 {type(rule).__name__!r}："
            f"新增规则类型时先扩展 parts.catalog._rule_dict")
    data = {"kind": type(rule).__name__}
    for f in dataclasses.fields(rule):
        data[f.name] = getattr(rule, f.name)
    return data


def _canonical_key(obj) -> str:
    """排序键：与 `_hash` 同一套 canonical JSON，保证"同内容同顺序"。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _part_electrical_dict(part: Part) -> dict:
    data = {
        "name": part.name, "category": part.category, "package": part.package,
        "spec": part.spec,
        "lifecycle": part.lifecycle, "lib_id": part.lib_id,
        "footprint": part.footprint,
        "allowed_footprints": list(part.allowed_footprints),
        "pins": [_pin_dict(p) for p in part.pins],
        "ratings": sorted(part.ratings), "operating": sorted(part.operating),
        "abs_max": sorted(part.abs_max),
        "supported_topologies": sorted(part.supported_topologies),
        "review_state": part.review_state,
        "manufacturer": part.manufacturer, "mpn": part.mpn,
        "sources": [_source_dict(s) for s in part.sources],
        # 规则是**合取**条件（每一条都必须满足），声明顺序无语义，故按
        # canonical 文本排序后再入哈希：顺序不同但内容相同的目录必须得到
        # 同一个 revision，否则重排一下规则就会让所有已验证的 IR 失效。
        "connection_rules": sorted(
            (_rule_dict(r) for r in part.connection_rules),
            key=_canonical_key),
    }
    if part.nominal is not None:
        data["nominal"] = {"kind": part.nominal.kind, "value": part.nominal.value,
                           "unit": part.nominal.unit}
    return data


def _part_supply_dict(part: Part) -> dict:
    return {"name": part.name, "price_cny": part.price_cny,
            "stock": part.stock, "lcsc": part.lcsc,
            "alternatives": list(part.alternatives)}


def _hash(data: dict) -> str:
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CatalogSnapshot:
    """不可变目录快照。构造后不再查全局 PARTS。"""
    id: str
    parts: Mapping[str, Part]
    revision: str           # 电气主数据哈希
    supply_revision: str    # 商业数据哈希（与上面解耦）
    name: str = "builtin"

    # ---- 构造 ---------------------------------------------------------------

    @classmethod
    def from_parts(cls, parts: Mapping[str, Part], *,
                   name: str = "builtin", id: str | None = None) -> "CatalogSnapshot":
        frozen = MappingProxyType(dict(parts))
        electrical = {n: _part_electrical_dict(p) for n, p in sorted(parts.items())}
        supply = {n: _part_supply_dict(p) for n, p in sorted(parts.items())}
        rev = _hash(electrical)
        return cls(id=id or f"{name}@{rev}", parts=frozen, revision=rev,
                   supply_revision=_hash(supply), name=name)

    @classmethod
    def builtin(cls) -> "CatalogSnapshot":
        """仓库自带目录（parts/partsdb.py 的 PARTS）。"""
        return cls.from_parts(PARTS, name="builtin")

    # ---- 查询 ---------------------------------------------------------------

    def get(self, name: str) -> Part | None:
        return self.parts.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.parts))

    def __len__(self) -> int:
        return len(self.parts)

    def __contains__(self, name: object) -> bool:
        return name in self.parts

    # ---- 自校验 -------------------------------------------------------------

    def validate(self, policy: CatalogValidationPolicy | None = None
                 ) -> Outcome:
        """目录记录自校验。返回诊断；`ok` 才表示目录可用于生成。

        `policy` 决定封装焊盘核对到什么程度（见 CatalogValidationPolicy）。
        不传 = 生成口径（没有读取器，未核对项进 `unverified`）。

        核对整个快照而不是"本次用到的记录"：目录是本次构建的**输入**，
        revision 也覆盖全部记录——一份带坏记录的目录，其证据链已经不完整，
        与这份 IR 恰好没用到那条记录无关。
        """
        if policy is None:
            policy = CatalogValidationPolicy()
        diags: list[Diagnostic] = []
        unverified: list[str] = []
        for name, part in sorted(self.parts.items()):
            self._check_part(name, part, policy, diags, unverified)
        return Outcome(tuple(diags), unverified=tuple(unverified))

    def _check_part(self, key: str, part: Part, policy: CatalogValidationPolicy,
                    diags: list[Diagnostic],
                    unverified: list[str]) -> None:
        path = f"parts[{key}]"

        def bad(code: str, message: str, **kw) -> None:
            diags.append(Diagnostic(code, "error", path, message, **kw))

        if key != part.name:
            bad("E-CAT-001", f"目录键 {key!r} 与记录名 {part.name!r} 不一致")
        if part.category not in CATEGORIES:
            bad("E-CAT-001", f"category {part.category!r} 不在 {CATEGORIES}")
        if part.review_state not in REVIEW_STATES:
            bad("E-CAT-001", f"review_state {part.review_state!r} 不在 {REVIEW_STATES}")
        if not part.pins:
            bad("E-CAT-001", "没有引脚表——无法生成任何连接")

        # 脚号唯一
        numbers = [p.number for p in part.pins]
        dupes = sorted({n for n in numbers if numbers.count(n) > 1})
        if dupes:
            diags.append(Diagnostic(
                "E-CAT-003", "error", f"{path}.pins",
                f"引脚编号重复: {dupes}"))

        # 引脚引用歧义：某个名字同时是另一个引脚的编号
        for p in part.pins:
            hits = part.pin_ambiguity(p.number)
            if hits:
                diags.append(Diagnostic(
                    "E-CAT-004", "error", f"{path}.pins",
                    f"引用 {p.number!r} 会同时命中引脚 {list(hits)}"))

        # 必接脚的 nc_condition 必须写明依据
        for p in part.pins:
            if not p.required and not p.nc_condition:
                bad("E-CAT-001",
                    f"引脚 {p.number}({p.name}) 非必接但没写 nc_condition")

        # 允许封装集合
        allowed = part.footprints()
        if part.footprint and allowed and part.footprint not in allowed:
            bad("E-CAT-001",
                f"默认封装 {part.footprint!r} 不在允许集合 {list(allowed)} 中")

        # sourced 记录必须有来源与页码
        if part.review_state == "sourced":
            if not part.sources:
                diags.append(Diagnostic(
                    "E-CAT-005", "error", f"{path}.sources",
                    "review_state=sourced 但没有来源记录"))
            if not part.manufacturer or not part.mpn:
                diags.append(Diagnostic(
                    "E-CAT-005", "error", path,
                    "review_state=sourced 但没有 manufacturer/mpn"))
            for src in part.sources:
                # 资料手册必须给页码；产品页/封装文件没有页码，但要写清
                # 它支撑了哪些字段（note 非空），否则等于没有出处。
                if src.kind == "datasheet":
                    if not src.pages or src.pages.strip() in ("-", ""):
                        diags.append(Diagnostic(
                            "E-CAT-005", "error", f"{path}.sources",
                            f"资料手册来源 {src.url} 没有页码"))
                elif not src.note.strip():
                    diags.append(Diagnostic(
                        "E-CAT-005", "error", f"{path}.sources",
                        f"来源 {src.url} 没写它支撑了什么（note 为空）"))
                if not src.revision.strip() or not src.retrieved.strip():
                    diags.append(Diagnostic(
                        "E-CAT-005", "error", f"{path}.sources",
                        f"来源 {src.url} 缺版本或复核日期"))

        # 封装焊盘核对。没有封装映射的器件（lib_id=None 的自绘符号）没有可核对
        # 的对象，不算"未核对"——那是目录明确声明"这块料没有 PCB 封装"。
        if part.footprint:
            self._check_pads(key, part, policy, diags, unverified)

    def _check_pads(self, key: str, part: Part, policy: CatalogValidationPolicy,
                    diags: list[Diagnostic],
                    unverified: list[str]) -> None:
        path = f"parts[{key}].footprint"
        symbol_pins = {p.number for p in part.pins}
        if policy.pad_provider is None:
            self._pads_not_checked(key, part, policy, diags, unverified,
                                   "没有封装读取器（本机没有 KiCad 封装库）")
            return
        try:
            pads = policy.pad_provider(part.footprint)
        except Exception as exc:                     # noqa: BLE001 - 读库失败要诊断
            self._pads_not_checked(key, part, policy, diags, unverified,
                                   f"封装读取失败：{exc}")
            return

        pad_numbers = {p.number for p in pads if p.number}
        missing = sorted(symbol_pins - pad_numbers)      # 符号有脚、封装无焊盘
        extra = sorted(pad_numbers - symbol_pins)        # 封装有焊盘、符号无脚
        if missing:
            diags.append(Diagnostic(
                "E-CAT-002", "error", path,
                f"符号引脚 {missing} 在封装 {part.footprint} 里没有同名焊盘"
                f"——这些脚在 PCB 上会悬空",
                stage="catalog", object_id=key,
                actual=f"符号脚 {sorted(symbol_pins)}",
                expected=f"封装焊盘 {sorted(pad_numbers)}",
                evidence=part.footprint))
        if extra:
            diags.append(Diagnostic(
                "E-CAT-002", "error", path,
                f"封装 {part.footprint} 的焊盘 {extra} 在符号里没有对应引脚"
                f"——这些焊盘在 PCB 上会没有网络",
                stage="catalog", object_id=key,
                actual=f"封装焊盘 {sorted(pad_numbers)}",
                expected=f"符号脚 {sorted(symbol_pins)}",
                evidence=part.footprint))

    def _pads_not_checked(self, key: str, part: Part,
                          policy: CatalogValidationPolicy,
                          diags: list[Diagnostic], unverified: list[str],
                          why: str) -> None:
        """焊盘映射没能核对。

        两条出口都由**策略**决定，不由异常类型决定：
        - `require_pads=True`：阻断（`E-CAT-006`）。核对不了就不能声称核对过。
        - 否则：`W-CAT-001` 提醒 + 记进 `unverified`。**未核对不是通过。**
        """
        path = f"parts[{key}].footprint"
        if policy.require_pads:
            diags.append(Diagnostic(
                "E-CAT-006", "error", path,
                f"本策略要求核对封装焊盘映射，但 {part.footprint} 核对不了"
                f"（{why}）——符号脚号是否与封装焊盘一致**未知**，"
                f"不能按已核对处理",
                stage="catalog", object_id=key, evidence=part.footprint))
            return
        diags.append(Diagnostic(
            "W-CAT-001", "warning", path,
            f"{why}，无法核对 {part.footprint} 的焊盘映射——该记录视为未核对",
            stage="catalog", object_id=key, evidence=part.footprint))
        unverified.append(f"{key}: 封装 {part.footprint} 的焊盘映射未核对"
                          f"（{why}）")

    # ---- 导出 ---------------------------------------------------------------

    def revision_of(self, part_name: str) -> str:
        """单个器件的电气主数据哈希（写进 BuildReport，便于追责）。"""
        part = self.parts.get(part_name)
        if part is None:
            return ""
        return _hash(_part_electrical_dict(part))

    def electrical_dict(self) -> dict:
        """整体电气主数据（落盘用，就是 revision 的原文）。"""
        return {n: _part_electrical_dict(p) for n, p in sorted(self.parts.items())}
