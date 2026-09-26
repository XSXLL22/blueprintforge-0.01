"""IR 校验器的回归测试。

示例 IR（examples/buck_12v_to_3v3.json）是「已核对的正例」：它本身必须
**0 错误**通过，且只允许出现下面登记过的警告。其余用例在它的副本上做
单点破坏，验证对应错误码。

为什么不是「0 错误 0 警告」：示例用的 MP1584EN 生命周期是不建议新设计
（NRFND，依据 MPS 厂牌产品页，见 parts/partsdb.py 的来源记录），所以
W-IR-PARTS-001 是**正确输出**，不是缺陷。原来的断言等于要求器件库把
NRFND 错记成 active——那是被本任务修掉的缺陷（审查报告 H5）。
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


#: 示例允许出现的警告（登记制：新警告必须在这里显式加一行，否则用例失败）。
EXAMPLE_EXPECTED_WARNINGS = {
    # MP1584 生命周期 NRFND——厂牌产品页，非缺陷
    "W-IR-PARTS-001",
    # 成本目标「<10 元」没有数量口径与计价货币——原需求就没给，
    # 于是它不满足「定义条件」的要求，只能停在"无法判定"，不能盖通过章
    "W-IR-DOC-004",
}

#: 目录层的警告：说的是"这份目录核对到什么程度"，不是"这份 IR 有什么问题"。
#: 它们随环境出现/消失（没有封装读取器时每条带封装的记录各一条），所以不
#: 并进上面那张"示例本身"的登记表——但它们同样**逐条计数**，不是放行。
CATALOG_LEVEL_WARNINGS = {"W-CAT-001"}


class TestExample(unittest.TestCase):
    def test_example_has_no_errors(self):
        out = validate(load(EXAMPLE))
        self.assertTrue(out.ok,
                        "\n".join(f"{d.code} {d.path}: {d.message}"
                                  for d in out.diagnostics))

    def test_example_warnings_are_registered(self):
        out = validate(load(EXAMPLE))
        catalog = [d for d in out.warnings if d.code in CATALOG_LEVEL_WARNINGS]
        self.assertEqual({d.code for d in out.warnings
                          if d.code not in CATALOG_LEVEL_WARNINGS},
                         EXAMPLE_EXPECTED_WARNINGS,
                         "\n".join(f"{d.code} {d.path}: {d.message}"
                                   for d in out.diagnostics))
        # 目录层警告不是"多出来的就登记一下"：生成口径（无封装读取器）下，
        # 恰好每条带封装的目录记录一条——数量对不上说明口径变了。
        self.assertEqual(len(catalog),
                         sum(1 for p in PARTS.values() if p.footprint))
        # 未核对不是错误：示例仍然 0 错误通过，但它也没被核对过。
        self.assertTrue(out.unverified, "生成口径必须留下未核对项")

    def test_example_roundtrip(self):
        """to_dict(from_dict(x)) 应能通过同样的校验。"""
        ir = load(EXAMPLE)
        out = validate(from_dict(ir.raw))
        self.assertTrue(out.ok,
                        "\n".join(f"{d.code} {d.path}: {d.message}"
                                  for d in out.diagnostics))


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
