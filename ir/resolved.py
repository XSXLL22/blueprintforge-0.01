"""ResolvedIR —— IR + 目录 + 全部解析结论，生成与校验共用的**唯一**输入。

为什么要有这一层：

1. **消灭第二套语义**。在此之前，`render()` 拿 `dict[str, Part]` 自己查一遍
   引脚、`validate()` 再查一遍，两处都得记得"引脚引用要先归一化脚名/脚号"
   "封装覆盖要判允许集"——只要有一处忘了，就会出现 T00 那种
   「符号用注入表、连线用全局表」。现在解析只有一次（`resolve()`），
   结论（连接、供电、有效封装、器件事实）全部落在这个不可变对象里。
2. **可追溯**。`catalog.revision` 与每个器件的电气主数据哈希都留在这里，
   BuildReport（T07）据此写清"这份产物是基于哪一版目录算出来的"。
3. **未核对项显式列出**。额定值没核到工况、目录缺字段时记 `unverified`，
   而不是当作"通过"——T00 的教训就是静默通过最危险。
4. **目录自校验的唯一起点**。`CatalogSnapshot.validate()` 只是"能调用的
   函数"，它必须在这里被调用才算生产闸门：校验器与生成器都走 `resolve()`，
   没有第二处调目录（复核报告 P1#1 就是"检查存在但生产路径没人调"）。

迁移点（有意保留，T05/T07 继续）：
  - `validate()` / `render()` 仍接受 `Mapping[str, Part]` 作为**入口参数**
    （测试与 CLI 的过渡形式），但入口即归一化成 `CatalogSnapshot`，
    下游只认 `ResolvedIR`，不再有第二处查目录的代码。
  - `ResolvedIR` 尚未写进 BuildReport（T07）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.catalog import CatalogSnapshot, CatalogValidationPolicy
from parts.partsdb import PARTS, Part

from checks.connectivity import Connectivity
from checks.preflight import preflight
from checks.supply import Supply


@dataclass(frozen=True)
class ResolvedComponent:
    """一个位号解析后的全部事实：选了什么料、量了什么值、用哪个封装。"""

    ref: str
    part_name: str
    part: Part
    role: str
    value: str | None
    #: **有效封装**（覆盖通过校验后才生效）；None = 该料没有封装映射
    footprint: str | None
    #: "override"（IR 覆盖生效）| "catalog"（用目录默认）| "none"
    footprint_source: str
    #: IR 里的额定值注释——只作说明，**不覆盖**目录额定值
    rating_note: str | None


@dataclass(frozen=True)
class ResolvedIR:
    """解析结论。构造后不再查目录，也不再重算连接。"""

    ir: PowerIR
    catalog: CatalogSnapshot
    components: tuple[ResolvedComponent, ...]
    connectivity: Connectivity
    supply: Supply
    #: 解析过程中产生的全部诊断（目录自校验/连接/供电/子图/器件事实）
    diagnostics: tuple[Diagnostic, ...]
    #: 未核对项：缺工况、缺目录字段、无法解析——**不是**"通过"
    unverified: tuple[str, ...] = ()
    #: 目录层未核对项：封装焊盘映射没能核对（缺读取器/读取失败）。
    #: 与 `unverified` 分开记：前者是"这份器件数据没核到"，这里是"这块料
    #: 在这个封装上到底是不是这些脚，没人读过封装文件"。BuildReport（T07）
    #: 要把它们列进 pending——`ok=True` 只说明"没有错误"，不说明"核对过"。
    catalog_unverified: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(d.is_error for d in self.diagnostics)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.is_error)

    def component(self, ref: str) -> ResolvedComponent | None:
        for c in self.components:
            if c.ref == ref:
                return c
        return None

    def footprint_of(self, ref: str) -> str | None:
        """该位号在原理图与网表里应当出现的封装。"""
        c = self.component(ref)
        return c.footprint if c is not None else None

    def part_of(self, ref: str) -> Part | None:
        c = self.component(ref)
        return c.part if c is not None else None


def as_catalog(parts: Mapping[str, Part] | CatalogSnapshot | None
               ) -> CatalogSnapshot:
    """入口归一化：None → 内置目录；字典 → 快照（带 revision）。"""
    if parts is None:
        return CatalogSnapshot.builtin()
    if isinstance(parts, CatalogSnapshot):
        return parts
    return CatalogSnapshot.from_parts(parts, name="injected")


def resolve(ir: PowerIR,
            parts: Mapping[str, Part] | CatalogSnapshot | None = None,
            *, catalog_policy: CatalogValidationPolicy | None = None
            ) -> ResolvedIR:
    """一次性解析：目录自校验 → 连接 → 供电 → 子图 → 器件事实。不抛异常。

    `catalog_policy` 决定封装焊盘核对到什么程度（见 CatalogValidationPolicy）。
    目录自校验**只在这里调一次**：校验器与生成器都从 `resolve()` 拿结论，
    不在各自的入口再写一套——复核报告 P1#1 的根因正是"检查存在，但生产
    路径没人调"。
    """
    catalog = as_catalog(parts)
    catalog_outcome = catalog.validate(catalog_policy)
    conn, supply, facts, diags = preflight(ir, catalog.parts)
    diags = list(catalog_outcome.diagnostics) + list(diags)

    components: list[ResolvedComponent] = []
    for comp in ir.components:
        part = catalog.get(comp.part)
        if part is None:
            continue                     # E-IR-PARTS-003 已报，这里不留半个记录
        components.append(ResolvedComponent(
            ref=comp.ref, part_name=part.name, part=part, role=comp.role,
            value=comp.value,
            footprint=facts.footprint_of(comp.ref),
            footprint_source=facts.footprint_sources.get(comp.ref, "none"),
            rating_note=comp.rating))

    return ResolvedIR(ir=ir, catalog=catalog,
                      components=tuple(components),
                      connectivity=conn, supply=supply,
                      diagnostics=tuple(diags), unverified=facts.unverified,
                      catalog_unverified=catalog_outcome.unverified)


def require_ok(rir: ResolvedIR, *, what: str = "生成") -> ResolvedIR:
    """有 error 就拒绝继续——生成器用这个，别自己判一遍。"""
    if rir.ok:
        return rir
    raise ValueError(
        f"连接/供电/子图/器件事实检查未通过，拒绝{what}：\n"
        + "\n".join(f"  {d.code} [{d.path}] {d.message}" for d in rir.errors))


#: 明确的内置目录（可读性用；等价于 as_catalog(None)）。
BUILTIN_PARTS = PARTS
