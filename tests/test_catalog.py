"""目录快照的两条生产闸门回归（复核报告 P1#1 / P1#2）。

**P1#1：目录自校验必须进生产路径。** `CatalogSnapshot.validate()` 早就实现了
引脚/来源/封装焊盘核对，但 `ir.validate.validate()` 与 `ir.resolved.resolve()`
都只调 `preflight()`，生成器又走 `resolve()`——于是"测试里验过的目录规则"
不是生成闸门：把 MP1584EN 的散热焊盘脚号写成 10，生产路径照样画出一张
`(pin "10" ...)` 的原理图。这里断言的是**生产入口**（validate / resolve /
write_schematic）自身会拦下来，而不是只有 `CatalogSnapshot.validate()` 被
手动调用时才知道。

**P1#2：revision 必须覆盖会改变电气结论的数据。** `revision` 是"这份产物
基于哪一版目录"的唯一证据。此前电气主数据哈希漏了 `connection_rules`
（数据手册要求的外围子图）与 `Source.note`（该来源支撑了哪些字段），删除
全部连接规则或改来源说明都得到同一个 revision——两份行为不同的目录无法区分。
这里逐字段做变形测试：改一个字段 revision 必须变；只动商业字段则只有
`supply_revision` 变。

两条都用**替身**封装读取器（`_stub_pads`），不依赖本机装没装 KiCad：同一个
输入在任何机器上都必须得到同一个结论（否则"通过"取决于环境）。真库只在
`test_builtin_catalog_passes_the_strict_policy` 里用，缺库就显式跳过。
"""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from backend.kicad.footprint import FootprintError, Pad
from backend.kicad.footprint import electrical_pads, footprints_root
from backend.kicad.schematic import render, write_schematic
from ir.resolved import resolve
from ir.schema import load
from ir.validate import validate
from parts.catalog import CatalogSnapshot, CatalogValidationPolicy
from parts.partsdb import PARTS, Part
from parts.rules import CapBetween, NotTiedToPin, RToNet, SeriesRCToNet

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"

#: SOIC-8-1EP 的电气焊盘号：1–8 与散热焊盘 9（见 tests/fixtures/mp1584_expected.json）。
_STUB_PAD_NUMBERS = tuple(str(n) for n in range(1, 10))


def _pad(number: str) -> Pad:
    return Pad(number=number, ptype="smd", shape="rect", at=(0.0, 0.0),
               size=(0.5, 1.0), layers=("F.Cu", "F.Paste", "F.Mask"),
               properties=())


def _stub_pads(lib_id: str) -> tuple[Pad, ...]:
    """替身封装读取器：给什么封装都返回 1–9 号电气焊盘。

    用替身而不是真库，是为了让"损坏目录能不能进生产路径"这条结论与本机
    有没有 KiCad 无关——依赖真库的用例在下面单独一条，并显式跳过。
    """
    return tuple(_pad(n) for n in _STUB_PAD_NUMBERS)


def _unreadable(lib_id: str) -> tuple[Pad, ...]:
    raise FootprintError(f"替身：封装库不可读（{lib_id}）")


def _strict_policy() -> CatalogValidationPolicy:
    """schematic 口径：带封装的器件必须真核对焊盘映射。"""
    return CatalogValidationPolicy(pad_provider=_stub_pads, require_pads=True)


def _generate_policy() -> CatalogValidationPolicy:
    """generate 口径：允许核对不了，但"未核对"必须显式记下来。"""
    return CatalogValidationPolicy(pad_provider=_stub_pads)


def _catalog(**overrides: Part) -> CatalogSnapshot:
    """内置目录 + 若干条被改坏的记录（名字即键，便于逐条替换）。"""
    return CatalogSnapshot.from_parts({**PARTS, **overrides})


def _codes(outcome) -> set[str]:
    return {d.code for d in outcome.errors}


def _ep10() -> Part:
    """MP1584EN 的散热焊盘脚号被写成 10——封装里其实是 9。

    这不只是"名字写错"：KiCad 靠脚号 ↔ 焊盘号连接，脚号 10 在 PCB 上会悬空，
    同时封装上的 9 号焊盘没有网络。
    """
    part = PARTS["MP1584EN"]
    return replace(part, pins=tuple(
        replace(p, number="10") if p.name == "EP" else p for p in part.pins))


class TestCatalogGateReachesProduction(unittest.TestCase):
    """P1#1：目录规则必须是生成闸门，不是"只能手动调用的函数"。"""

    @classmethod
    def setUpClass(cls):
        cls.ir = load(EXAMPLE)

    def _incomplete(self) -> dict[str, Part]:
        """结构错误：SW 被改成非必接却又不写 nc_condition（E-CAT-001）。"""
        part = PARTS["MP1584EN"]
        return {**PARTS, "MP1584EN": replace(part, pins=tuple(
            replace(p, required=False, nc_condition="") if p.name == "SW" else p
            for p in part.pins))}

    def test_incomplete_record_blocks_the_production_path(self):
        """与封装库无关的目录结构错误也必须拦路（不需要任何读取器）。"""
        broken = self._incomplete()
        self.assertIn("E-CAT-001", _codes(validate(self.ir, broken)),
                      "校验器没有跑目录结构自校验")
        rir = resolve(self.ir, broken)
        self.assertIn("E-CAT-001", _codes(rir), "解析结论里没有目录结构诊断")
        self.assertFalse(rir.ok)

    def test_wrong_heat_sink_pin_is_caught_at_every_entry_point(self):
        """EP=10：validate / resolve / 生成 三个入口都必须得到 E-CAT-002。"""
        catalog = _catalog(MP1584EN=_ep10())
        policy = _strict_policy()

        self.assertIn("E-CAT-002", _codes(validate(self.ir, catalog, catalog_policy=policy)))

        rir = resolve(self.ir, catalog, catalog_policy=policy)
        self.assertIn("E-CAT-002", _codes(rir))
        self.assertFalse(rir.ok, "脚号与焊盘不符的目录不得判为可用")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "build"
            with self.assertRaises(ValueError) as ctx:
                write_schematic(self.ir, out_dir, catalog, catalog_policy=policy)
            self.assertIn("E-CAT-002", str(ctx.exception))
            self.assertEqual(list(out_dir.glob("*.kicad_sch")), [],
                             "被拒的目录不得留下任何原理图产物")

    def test_render_also_refuses(self):
        """渲染入口与落盘入口同一份判定（不能"校验一套、生成一套"）。"""
        with self.assertRaises(ValueError) as ctx:
            render(self.ir, _catalog(MP1584EN=_ep10()), catalog_policy=_strict_policy())
        self.assertIn("E-CAT-002", str(ctx.exception))

    def test_validator_and_generator_share_one_catalog_rule_set(self):
        """两个入口的目录诊断必须逐码一致——规则只有一份。"""
        catalog = _catalog(MP1584EN=_ep10())
        policy = _strict_policy()
        from_validate = {d.code for d in validate(self.ir, catalog, catalog_policy=policy).diagnostics
                         if "-CAT-" in d.code}
        from_resolve = {d.code for d in resolve(self.ir, catalog, catalog_policy=policy).diagnostics
                        if "-CAT-" in d.code}
        self.assertEqual(from_validate, from_resolve)
        self.assertEqual(from_validate, {"E-CAT-002"})


class TestCatalogValidationPolicy(unittest.TestCase):
    """核对不到 ≠ 通过：策略决定"核对不到"是阻断还是进未核对清单。"""

    @classmethod
    def setUpClass(cls):
        cls.ir = load(EXAMPLE)

    def test_missing_reader_is_blocking_under_the_strict_policy(self):
        out = validate(self.ir, _catalog(MP1584EN=_ep10()),
                       catalog_policy=CatalogValidationPolicy(require_pads=True))
        self.assertIn("E-CAT-006", _codes(out),
                      "没有封装读取器时不能既说 schematic 核对过、又只给 warning")

    def test_unreadable_footprint_is_blocking_under_the_strict_policy(self):
        """读取失败（封装库缺这个封装/文件坏了）是阻断，不是成功里的 warning。"""
        out = validate(self.ir, _catalog(MP1584EN=_ep10()),
                       catalog_policy=CatalogValidationPolicy(
                           pad_provider=_unreadable, require_pads=True))
        self.assertIn("E-CAT-006", _codes(out))
        self.assertNotIn("W-CAT-001", {d.code for d in out.warnings},
                         "严格口径下不该同时给出一个 warning 版结论")

    def test_mismatch_is_an_error_even_when_pads_are_optional(self):
        """策略只管"核对不到"怎么办；真核出来不符，宽松口径下同样是错误。"""
        out = validate(self.ir, _catalog(MP1584EN=_ep10()),
                       catalog_policy=_generate_policy())
        self.assertIn("E-CAT-002", _codes(out))

    def test_generate_policy_records_unverified_instead_of_passing(self):
        """generate 口径：能核对就核对，核对不了记未核对——不是"通过"。"""
        rir = resolve(self.ir, _catalog(MP1584EN=_ep10()),
                      catalog_policy=CatalogValidationPolicy())
        self.assertTrue(rir.ok, "仅生成口径不该因为缺 KiCad 封装库就失败")
        self.assertIn("W-CAT-001", {d.code for d in rir.diagnostics})
        joined = " | ".join(rir.catalog_unverified)
        self.assertIn("MP1584EN", joined, "未核对项必须能追到具体记录")
        self.assertIn(PARTS["MP1584EN"].footprint, joined)

    def test_builtin_catalog_passes_the_strict_policy(self):
        """反例的反面：真库 + 内置目录必须过——否则闸门只会误报。"""
        if footprints_root() is None:
            self.skipTest("本机没有 KiCad 封装库")
        out = validate(self.ir, catalog_policy=CatalogValidationPolicy(
            pad_provider=electrical_pads, require_pads=True))
        self.assertTrue(out.ok, out.render())


class TestCatalogRevision(unittest.TestCase):
    """P1#2：revision 是"用了哪套电气规则"的唯一证据，字段变则必须变。"""

    def setUp(self):
        self.base = CatalogSnapshot.builtin()
        self.part = PARTS["MP1584EN"]

    def _rev(self, part: Part) -> str:
        return _catalog(**{part.name: part}).revision

    def _rule_mutations(self):
        """每条都必须改变 revision：规则的类型/引脚/目标网/依据都在内。"""
        rules = self.part.connection_rules
        yield "删掉全部规则", replace(self.part, connection_rules=())
        yield "少一条规则", replace(self.part, connection_rules=rules[:-1])
        yield "规则顺序之外多一条", replace(
            self.part, connection_rules=rules + (RToNet(
                pin="EN", net="GND", evidence="同上"),))
        yield "CapBetween 的引脚改了", replace(self.part, connection_rules=(
            replace(rules[0], pin_a="VIN"),) + rules[1:])
        yield "RToNet 的目标网改了", replace(self.part, connection_rules=(
            rules[0], replace(rules[1], net="VOUT")) + rules[2:])
        yield "SeriesRCToNet 的引脚改了", replace(self.part, connection_rules=(
            rules[:2] + (replace(rules[2], pin="FB"),) + rules[3:]))
        yield "NotTiedToPin 的 why 改了", replace(self.part, connection_rules=(
            rules[:3] + (replace(rules[3], why="随便写的"),)))
        yield "规则依据（页码）改了", replace(self.part, connection_rules=(
            replace(rules[0], evidence="MP1584 Rev1.0 第 99 页"),) + rules[1:])
        yield "规则类型换了", replace(self.part, connection_rules=(
            RToNet(pin=rules[0].pin_a, net=rules[0].pin_b,
                   evidence=rules[0].evidence),) + rules[1:])

    def test_connection_rule_changes_are_all_visible(self):
        for label, mutated in self._rule_mutations():
            with self.subTest(mutation=label):
                self.assertNotEqual(self._rev(mutated), self.base.revision, label)

    def test_rule_order_is_not_part_of_the_revision(self):
        """规则是合取条件，顺序无语义：排序规则要明确，顺序不该造出假变化。"""
        shuffled = tuple(reversed(self.part.connection_rules))
        self.assertEqual(self._rev(replace(self.part, connection_rules=shuffled)),
                         self.base.revision)

    def test_source_note_is_part_of_the_revision(self):
        """note 说明该来源支撑了哪些字段——它变了，证据含义就变了。"""
        sources = list(self.part.sources)
        sources[0] = replace(sources[0], note=sources[0].note + "（追加说明）")
        self.assertNotEqual(self._rev(replace(self.part, sources=tuple(sources))),
                            self.base.revision)

    def test_source_pages_and_revision_are_part_of_the_revision(self):
        sources = list(self.part.sources)
        for field, value in (("pages", "99"), ("revision", "Rev 9.9"),
                             ("url", "https://example.invalid/x"),
                             ("kind", "series"), ("retrieved", "1999-01-01")):
            sources[0] = replace(self.part.sources[0], **{field: value})
            with self.subTest(field=field):
                self.assertNotEqual(
                    self._rev(replace(self.part, sources=tuple(sources))),
                    self.base.revision)

    def test_pin_fields_are_part_of_the_revision(self):
        for field, value in (("number", "99"), ("name", "SW_X"),
                             ("kind", "input"), ("required", False),
                             ("nc_condition", "手册第 4 页")):
            pins = tuple(replace(p, **{field: value}) if p.number == "1" else p
                         for p in self.part.pins)
            with self.subTest(field=field):
                self.assertNotEqual(self._rev(replace(self.part, pins=pins)),
                                    self.base.revision)

    def test_ratings_limits_footprint_and_spec_are_part_of_the_revision(self):
        cases = {
            "ratings": replace(self.part, ratings=self.part.ratings + (("x", 1.0),)),
            "operating": replace(self.part, operating=(("vin_min", 9.9),)),
            "abs_max": replace(self.part, abs_max=(("vin", 1.0),)),
            "footprint": replace(self.part, footprint="Package_SO:SomethingElse"),
            "allowed_footprints": replace(self.part, allowed_footprints=()),
            "supported_topologies": replace(self.part, supported_topologies=()),
            "spec（选型依据的一句话）": replace(self.part, spec="改过的摘要"),
            "lifecycle": replace(self.part, lifecycle="active"),
            "review_state": replace(self.part, review_state="generic"),
            "lib_id": replace(self.part, lib_id="Device:Q_NMOS_GSD"),
            "manufacturer": replace(self.part, manufacturer="别人家"),
            "mpn": replace(self.part, mpn="OTHER-MPN"),
        }
        for label, mutated in cases.items():
            with self.subTest(field=label):
                self.assertNotEqual(self._rev(mutated), self.base.revision, label)

    def test_supply_only_changes_leave_the_electrical_revision_alone(self):
        """一次价格刷新不该让所有已验证的 IR 变成"目录变了"。"""
        part = replace(self.part, price_cny=9.9, stock=1, lcsc="C99999",
                       alternatives=("MP2338", "MP2315"))
        snap = _catalog(MP1584EN=part)
        self.assertEqual(snap.revision, self.base.revision)
        self.assertNotEqual(snap.supply_revision, self.base.supply_revision)

    def test_revision_does_not_depend_on_mapping_order(self):
        items = list(PARTS.items())
        self.assertEqual(CatalogSnapshot.from_parts(dict(items)).revision,
                         CatalogSnapshot.from_parts(dict(reversed(items))).revision)

    def test_snapshot_carries_a_readable_label_next_to_the_hash(self):
        """审计证据不能只有一个短 hash：可读的名字/版本要一起能取到。"""
        self.assertEqual(self.base.name, "builtin")
        self.assertTrue(self.base.revision)
        self.assertIn(self.base.name, self.base.id)
        self.assertIn(self.base.revision, self.base.id)

    def test_rule_serialization_covers_every_declared_field(self):
        """规则字段是"数据手册要求"，漏一个就等于那份要求不进证据。

        直接对序列化函数断言：新增规则类型/字段时，这条会先把人叫醒。
        """
        from parts.catalog import _rule_dict
        for rule in self.part.connection_rules:
            with self.subTest(rule=type(rule).__name__):
                got = _rule_dict(rule)
                self.assertEqual(got["kind"], type(rule).__name__)
                self.assertEqual(
                    set(got) - {"kind"},
                    {f.name for f in dataclasses.fields(rule)})

    def test_rule_serialization_refuses_unknown_objects(self):
        """不是数据类就报错——不许把一个来路不明的对象悄悄哈希成常量。"""
        from parts.catalog import _rule_dict
        with self.assertRaises(TypeError):
            _rule_dict("BST 与 SW 之间接电容")


if __name__ == "__main__":
    unittest.main()
