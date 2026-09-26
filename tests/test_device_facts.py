"""器件事实的独立核对（对应审查报告 H1/H5 与「验证器与生成器共用同一份
未经独立校准的事实」这一核心短板）。

预期值来自 `tests/fixtures/mp1584_expected.json`——手工按原厂资料录入，
**不从 parts/partsdb.py 生成**。这样目录改错时测试会失败，而不是跟着一起错。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.kicad.footprint import (FootprintError, electrical_pads,
                                     footprints_root, pads_of)
from parts.catalog import CatalogSnapshot, CatalogValidationPolicy
from parts.partsdb import PARTS
from parts.values import (MATCH_CLOSE, MATCH_EXACT, Measurement,
                          magnitudes_match, parse_magnitude)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "mp1584_expected.json"


def _expected() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestMp1584Facts(unittest.TestCase):
    """目录 vs 原厂资料（独立预期值）。"""

    def setUp(self):
        self.exp = _expected()
        self.part = PARTS[self.exp["part"]]

    def test_pin_numbers_and_names(self):
        """脚号→脚名映射必须与第 2/4 页一致（H1 的核心）。"""
        got = [{"number": p.number, "name": p.name} for p in self.part.pins]
        self.assertEqual(got, self.exp["pins"],
                         "引脚表与数据手册不符——注意封装焊盘号必须与脚号"
                         "一一对应，改脚号前先读实际封装")

    def test_manufacturer_and_mpn(self):
        self.assertEqual(self.part.manufacturer, self.exp["manufacturer"])
        self.assertEqual(self.part.mpn, self.exp["mpn"])

    def test_lifecycle_is_nrfnd(self):
        """H5：厂牌产品页为 NRFND（不是 active，也不是 eol）。"""
        self.assertEqual(self.part.lifecycle, self.exp["lifecycle"])

    def test_operating_range(self):
        for key, want in self.exp["operating"].items():
            self.assertEqual(self.part.limit(key), want, key)

    def test_absolute_max_separate_from_operating(self):
        """绝对最大值不能混进推荐工作范围（EN 的 6V 尤其）。"""
        for key, want in self.exp["abs_max"].items():
            self.assertEqual(self.part.absolute_max(key), want, key)
            self.assertNotIn(key, dict(self.part.operating),
                             f"{key} 是绝对最大值，不应出现在推荐工作范围")

    def test_electrical_values(self):
        for key, want in self.exp["electrical"].items():
            self.assertEqual(self.part.rating(key), want, key)

    def test_sw_abs_max_not_a_constant(self):
        """SW 绝对最大值是 VIN+0.3V，写成常数就是编造。"""
        self.assertIsNone(self.part.absolute_max("sw"))
        self.assertTrue(any("VIN+0.3V" in s.note for s in self.part.sources))

    def test_sourced_record_has_traceable_sources(self):
        self.assertEqual(self.part.review_state, "sourced")
        pages = {s.pages for s in self.part.sources if s.kind == "datasheet"}
        for want in ("2", "3", "4", "9", "10", "12,13", "14"):
            self.assertIn(want, pages, f"缺少第 {want} 页的来源记录")


class TestMp1584Footprint(unittest.TestCase):
    """封装焊盘核对：散热焊盘不能凭经验写成 EP 或 9，必须读实际封装。"""

    def setUp(self):
        self.exp = _expected()

    def test_electrical_pads_match_expectation(self):
        try:
            pads = electrical_pads(self.exp["footprint"])
        except FootprintError as exc:
            self.skipTest(f"本机没有 KiCad 封装库：{exc}")
        numbers = sorted({p.number for p in pads})
        self.assertEqual(numbers, sorted(self.exp["footprint_electrical_pads"]))

    def test_heatsink_pad_number_and_size(self):
        try:
            pads = pads_of(self.exp["footprint"])
        except FootprintError as exc:
            self.skipTest(f"本机没有 KiCad 封装库：{exc}")
        sinks = [p for p in pads if p.is_heatsink]
        self.assertEqual(len(sinks), 1, "应当恰好有一个散热焊盘")
        self.assertEqual(sinks[0].number, self.exp["footprint_heatsink_pad"])
        self.assertEqual(list(sinks[0].size), self.exp["footprint_heatsink_size_mm"])

    def test_unnamed_paste_pads_are_not_electrical(self):
        """4 个无编号钢网焊盘不是电气焊盘——不能计入「引脚数」。"""
        try:
            pads = pads_of(self.exp["footprint"])
        except FootprintError as exc:
            self.skipTest(f"本机没有 KiCad 封装库：{exc}")
        unnamed = [p for p in pads if not p.number]
        self.assertEqual(len(unnamed), 4)
        self.assertFalse(any(p.is_electrical for p in unnamed))

    def test_catalog_pin_table_covers_all_electrical_pads(self):
        """目录引脚号集合必须与封装电气焊盘号集合相等（含散热焊盘）。"""
        part = PARTS[self.exp["part"]]
        try:
            pads = electrical_pads(part.footprint)
        except FootprintError as exc:
            self.skipTest(f"本机没有 KiCad 封装库：{exc}")
        self.assertEqual(sorted(p.number for p in part.pins),
                         sorted({p.number for p in pads}))

    def test_catalog_selfcheck_catches_wrong_heatsink_number(self):
        """反例：把散热焊盘脚号写成 10 必须被目录自校验拦住。

        先判"本机有没有封装库"再 skip，不再用 try/except 捕 `FootprintError`：
        读取失败如今是**诊断**（由策略决定是阻断还是记未核对），`validate()`
        不再抛异常——旧写法的 except 永远捕不到，测试随环境时对时错（复核
        报告 P1#1 的附带项：测试意图要与实际 API 一致）。

        口径用 schematic（`require_pads=True`）：核对不了就是错误，不允许
        降级成"未核对"蒙混过关。
        """
        import dataclasses
        if footprints_root() is None:
            self.skipTest("本机没有 KiCad 封装库")
        part = PARTS[self.exp["part"]]
        broken = dataclasses.replace(part, pins=tuple(
            dataclasses.replace(p, number="10") if p.name == "EP" else p
            for p in part.pins))
        snap = CatalogSnapshot.from_parts({**PARTS, part.name: broken})
        out = snap.validate(CatalogValidationPolicy(
            pad_provider=electrical_pads, require_pads=True))
        self.assertFalse(out.ok)
        self.assertIn("E-CAT-002", {d.code for d in out.errors})
        self.assertEqual(out.unverified, (),
                         "核对得了就必须真核对：这里不许出现「未核对」出口")


class TestBuiltinCatalog(unittest.TestCase):
    def test_every_part_has_valid_shape(self):
        out = CatalogSnapshot.builtin().validate()
        self.assertFalse([d for d in out.errors if d.code == "E-CAT-001"],
                         out.render())

    def test_generic_parts_are_not_claimed_as_sourced(self):
        """泛化料号不得自称已核对——否则等于把占位数据伪装成事实。"""
        for name, part in PARTS.items():
            with self.subTest(part=name):
                self.assertIn(part.review_state, ("sourced", "generic"))
                if part.review_state == "sourced":
                    self.assertTrue(part.sources, f"{name} 自称已核对但没有来源")
                    self.assertTrue(part.mpn, f"{name} 自称已核对但没有 MPN")

    def test_resistor_and_capacitor_values_are_parseable(self):
        """目录里的标称值必须能被单位解析器读出来（否则一致性检查无从谈起）。"""
        for name, part in PARTS.items():
            if part.nominal is None:
                continue
            with self.subTest(part=name):
                self.assertGreater(part.nominal.value, 0.0)
                self.assertEqual(part.nominal.kind, part.nominal.kind.lower())


class TestValueParsing(unittest.TestCase):
    def test_common_forms(self):
        cases = [
            ("49.9k", "resistance", 49.9e3), ("4R7", "resistance", 4.7),
            ("1K5", "resistance", 1.5e3), ("10M", "resistance", 10e6),
            ("100nF", "capacitance", 100e-9), ("0.1uF", "capacitance", 0.1e-6),
            ("0.1µF", "capacitance", 0.1e-6), ("220pF", "capacitance", 220e-12),
            ("10uH", "inductance", 10e-6), ("4.7µH", "inductance", 4.7e-6),
            ("50mV", "voltage", 0.05), ("10元", "cost", 10.0),
        ]
        for text, dim, want in cases:
            with self.subTest(text=text):
                got = parse_magnitude(text)
                self.assertIsInstance(got, Measurement)
                self.assertEqual(got.dimension, dim)
                self.assertAlmostEqual(got.value, want, places=12)

    def test_ratio(self):
        got = parse_magnitude("85%")
        self.assertEqual(got.dimension, "ratio")
        self.assertAlmostEqual(got.value, 0.85)

    def test_refuses_to_guess(self):
        """解析不了就返回 None，绝不退化成 0 或默认值。"""
        for text in ("", "not-a-number", "abc", "10x", None, 3.3, "NaN", "Infinity"):
            with self.subTest(text=text):
                self.assertIsNone(parse_magnitude(text))

    def test_matching(self):
        self.assertEqual(magnitudes_match(49.9e3, 49.9e3), MATCH_EXACT)
        self.assertEqual(magnitudes_match(50e3, 49.9e3), MATCH_CLOSE)
        self.assertEqual(magnitudes_match(10e3, 49.9e3), "differ")


if __name__ == "__main__":
    unittest.main()
