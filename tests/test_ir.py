"""IR 校验器的回归测试。

示例 IR（examples/buck_12v_to_3v3.json）是「干净模板」：它本身必须
0 错误 0 警告通过。其余用例在它的副本上做单点破坏，验证对应错误码。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ir.schema import from_dict, load
from ir.validate import validate
from parts.partsdb import PARTS, Part

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"


def _raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _codes(out, severity: str) -> list[str]:
    return [d.code for d in out.diagnostics if d.severity == severity]


class TestExample(unittest.TestCase):
    def test_example_is_clean(self):
        out = validate(load(EXAMPLE))
        self.assertEqual(out.diagnostics, (),
                         "\n".join(f"{d.code} {d.path}: {d.message}"
                                   for d in out.diagnostics))

    def test_example_roundtrip(self):
        """to_dict(from_dict(x)) 应能通过同样的校验。"""
        ir = load(EXAMPLE)
        out = validate(from_dict(ir.raw))
        self.assertEqual(out.diagnostics, ())


class TestStructure(unittest.TestCase):
    def test_wrong_schema_version(self):
        raw = _raw()
        raw["schema_version"] = "9.9"
        self.assertIn("E-IR-STRUCT-001", _codes(validate(from_dict(raw)), "error"))

    def test_topology_mismatch(self):
        raw = _raw()
        raw["topology"] = "ldo"
        self.assertIn("E-IR-STRUCT-004", _codes(validate(from_dict(raw)), "error"))


class TestElectrical(unittest.TestCase):
    def test_vin_not_above_vout(self):
        raw = _raw()
        # 整段换成排序合法、但上限低于 vout.max 的输入，精确触发 E-IR-ELECT-002
        raw["electrical"]["vin"] = {"min": 2.0, "typ": 2.5, "max": 3.2}
        self.assertIn("E-IR-ELECT-002", _codes(validate(from_dict(raw)), "error"))

    def test_range_order(self):
        raw = _raw()
        raw["electrical"]["vout"]["typ"] = 0.1
        self.assertIn("E-IR-ELECT-001", _codes(validate(from_dict(raw)), "error"))

    def test_ldo_heat_warning(self):
        raw = _raw()
        raw["module_type"] = "power.ldo"
        raw["topology"] = "ldo"
        raw["components"][0]["part"] = "AMS1117-3.3"
        out = validate(from_dict(raw))
        self.assertIn("W-IR-ELECT-001", _codes(out, "warning"))


class TestParts(unittest.TestCase):
    def test_unknown_part_is_error(self):
        raw = _raw()
        raw["components"][0]["part"] = "NE555"
        self.assertIn("E-IR-PARTS-003", _codes(validate(from_dict(raw)), "error"))

    def test_eol_part_is_hard_error(self):
        parts = dict(PARTS, DINOCHIP=Part(
            name="DINOCHIP", category="regulator", package="DIP-8",
            spec="已停产演示件", lifecycle="eol", price_cny=1.0, stock=1))
        raw = _raw()
        raw["components"][0]["part"] = "DINOCHIP"
        self.assertIn("E-IR-PARTS-004", _codes(validate(from_dict(raw), parts), "error"))

    def test_role_category_mismatch(self):
        raw = _raw()
        raw["components"][1]["role"] = "diode"   # L1 是电感
        self.assertIn("E-IR-PARTS-008", _codes(validate(from_dict(raw)), "error"))

    def test_duplicate_ref(self):
        raw = _raw()
        raw["components"][2]["ref"] = "U1"
        self.assertIn("E-IR-PARTS-001", _codes(validate(from_dict(raw)), "error"))

    def test_buck_without_inductor(self):
        raw = _raw()
        raw["components"] = [c for c in raw["components"] if c["role"] != "inductor"]
        self.assertIn("E-IR-PARTS-007", _codes(validate(from_dict(raw)), "error"))


class TestNets(unittest.TestCase):
    def test_dangling_pin(self):
        raw = _raw()
        raw["nets"][0]["pins"].append({"ref": "U9", "pin": "1"})
        self.assertIn("E-IR-NETS-004", _codes(validate(from_dict(raw)), "error"))

    def test_missing_gnd(self):
        raw = _raw()
        raw["nets"] = [n for n in raw["nets"] if n["name"] != "GND"]
        self.assertIn("E-IR-NETS-002", _codes(validate(from_dict(raw)), "error"))


class TestDocumentation(unittest.TestCase):
    def test_missing_rationale_is_warning_only(self):
        raw = _raw()
        raw["design_rationale"] = ""
        out = validate(from_dict(raw))
        self.assertTrue(out.ok)              # 警告不拦路
        self.assertIn("W-IR-DOC-001", _codes(out, "warning"))


if __name__ == "__main__":
    unittest.main()
