"""器件事实核对：有效封装、value 与所选料是否一致、额定值 vs 需求、拓扑支持。

对应 T00 复现出来的四个**假通过**（`ok=true, codes=[]`）：

    component_ratings_ignored      需求远超器件额定值 → 通过
    regulator_topology_mismatch    异步芯片当同步 buck 用 → 通过
    async_buck_without_diode       缺续流二极管 → 通过
    ignored_footprint_override     写了封装覆盖，图纸/网表里却是另一个 → 覆盖"生效"了

判据同样是**行为**：多数用例既断言诊断码，也断言生成器拒绝产出；
覆盖类用例还断言有效封装真的出现在生成的原理图里。

这里的目录是**合成审计件**（名字带 AUDIT- 前缀、spec 里写明非真实器件）——
它只用来把额定值/量程摆到边界上，不声称任何真实型号的参数。
真实器件的核对在 `parts/partsdb.py` 的 sources 里，不在这里造。
"""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from backend.kicad.schematic import render
from ir.resolved import resolve
from ir.schema import from_dict
from ir.validate import validate
from parts.partsdb import PARTS, Param, Part, Pin

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"

ALT_FOOTPRINT = "Audit:AltFootprint"          # 在允许集合内
DENIED_FOOTPRINT = "Audit:RequestedFootprint"  # 不在允许集合内

_REG = Part(
    name="AUDIT-REG-5V", category="regulator", package="SOT-23-3",
    spec="审计用合成稳压器（非真实器件，仅用于边界用例）", lifecycle="active",
    footprint="Package_TO_SOT_SMD:SOT-23-3",
    allowed_footprints=("Package_TO_SOT_SMD:SOT-23-3", ALT_FOOTPRINT),
    pins=(Pin("1", "VIN", "power_in"), Pin("2", "GND", "power_in"),
          Pin("3", "VOUT", "power_out")),
    operating=(("vin_min", 4.5), ("vin_max", 5.5), ("iout_max", 1.0)),
    abs_max=(("vin", 6.0),),
    supported_topologies=("ldo",),
)

_CAP = Part(
    name="AUDIT-CAP-1U", category="capacitor", package="0603",
    spec="审计用合成电容（非真实器件）", lifecycle="active",
    footprint="Capacitor_SMD:C_0603_1608Metric",
    pins=(Pin("1", "1", "passive"), Pin("2", "2", "passive")),
    nominal=Param("capacitance", 1e-6, "F"),
    ratings=(("voltage_v", 6.3),),
)

_CATALOG = {"AUDIT-REG-5V": _REG, "AUDIT-CAP-1U": _CAP}


def _cap(**over) -> Part:
    return replace(_CAP, **over)


def _reg(**over) -> dict:
    """只换稳压器的目录；电容保持原样，用例里只动一个变量。"""
    return {**_CATALOG, "AUDIT-REG-5V": replace(_REG, **over)}


def _raw() -> dict:
    """LDO + 两只电容：C1 跨 VIN–GND（电压已知），C2 跨 VOUT–GND（VOUT 未声明）。"""
    return {
        "schema_version": "0.2", "project": "audit", "module_type": "power.ldo",
        "topology": "ldo",
        "electrical": {"vin": {"min": 4.5, "typ": 5.0, "max": 5.5},
                       "vout": {"min": 3.2, "typ": 3.3, "max": 3.4},
                       "iout_max": 0.5},
        "components": [
            {"ref": "U1", "part": "AUDIT-REG-5V", "role": "regulator"},
            {"ref": "C1", "part": "AUDIT-CAP-1U", "role": "capacitor",
             "value": "1uF"},
            {"ref": "C2", "part": "AUDIT-CAP-1U", "role": "capacitor"},
        ],
        "nets": [
            {"name": "VIN", "netclass": "power", "pins": [
                {"ref": "U1", "pin": "VIN"}, {"ref": "C1", "pin": "1"}]},
            {"name": "VOUT", "netclass": "power", "pins": [
                {"ref": "U1", "pin": "VOUT"}, {"ref": "C2", "pin": "1"}]},
            {"name": "GND", "netclass": "power", "pins": [
                {"ref": "U1", "pin": "GND"},
                {"ref": "C1", "pin": "2"}, {"ref": "C2", "pin": "2"}]},
        ],
        "targets": {},
        "ports": [
            {"name": "VIN_IN", "direction": "input", "net": "VIN", "drives": True},
            {"name": "GND_REF", "direction": "bidirectional", "net": "GND",
             "drives": True},
        ],
        "assumptions": ["审计用例，无实际用途"], "risks": ["无"],
        "design_rationale": "把额定值与需求量摆到边界上，验证是否真的核对",
    }


def _codes(raw: dict, parts=None) -> set[str]:
    """**错误**码：判"是否被拒绝"用这个（warning 不该拦路）。"""
    return {d.code for d in validate(from_dict(raw), parts).errors}


def _all_codes(raw: dict, parts=None) -> set[str]:
    return {d.code for d in validate(from_dict(raw), parts).diagnostics}


#: 目录层"未核对"提醒：本机没有封装读取器时，注入目录里每条带封装的记录
#: 各记一条 `W-CAT-001`。它说的是**目录核对到什么程度**，不是这份 IR 的语义
#: 结论；下面的集合断言关心的是后者，故显式剔除——按**码**剔除，别的任何
#: 新警告仍然会让断言失败（不是放宽，是把口径分清楚）。
CATALOG_LEVEL_CODES = {"W-CAT-001"}


def _comp(raw: dict, ref: str) -> dict:
    return next(c for c in raw["components"] if c["ref"] == ref)


def _refuses(raw: dict, parts=None) -> str:
    """生成器必须拒绝：返回拒绝原因，若照常产出则用例失败。"""
    try:
        render(from_dict(raw), parts)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("生成器在器件事实不成立时仍产出了原理图")


class TestRatingsAgainstDemand(unittest.TestCase):
    """需求超出器件额定值/工作范围必须报错——注释不能把额定值改大。"""

    def test_capacitor_over_its_voltage_rating_is_rejected(self):
        parts = {**_CATALOG, "AUDIT-CAP-1U": _cap(ratings=(("voltage_v", 4.0),))}
        raw = _raw()
        self.assertIn("E-IR-ELECT-004", _codes(raw, parts))
        self.assertIn("E-IR-ELECT-004", _refuses(raw, parts))

    def test_capacitor_within_rating_passes(self):
        raw = _raw()                       # 6.3V > VIN 5.5V
        self.assertEqual(_codes(raw, _CATALOG), set())

    def test_rating_annotation_cannot_lift_the_catalog_limit(self):
        """目录说 4V、注释说 50V：注释不生效，用量仍按目录判。"""
        parts = {**_CATALOG, "AUDIT-CAP-1U": _cap(ratings=(("voltage_v", 4.0),))}
        raw = _raw()
        _comp(raw, "C1")["rating"] = "50V"
        got = _codes(raw, parts)
        self.assertIn("E-IR-ELECT-004", got, "注释把耐压'改大'后就不判了")
        self.assertIn("E-IR-PARTS-015", got, "注释与目录矛盾本身也要报")

    def test_rating_annotation_matching_catalog_is_clean(self):
        raw = _raw()
        _comp(raw, "C1")["rating"] = "6.3V"
        self.assertEqual(_codes(raw, _CATALOG), set())

    def test_regulator_demand_outside_operating_range(self):
        parts = _reg(operating=(("vin_min", 8.0), ("vin_max", 9.0),
                                ("iout_max", 1.0)))
        raw = _raw()
        self.assertIn("E-IR-ELECT-004", _codes(raw, parts))

    def test_regulator_demand_near_the_limit_warns(self):
        raw = _raw()
        raw["electrical"]["iout_max"] = 0.95      # 推荐 1.0A 的 95%
        self.assertIn("W-IR-ELECT-002", _all_codes(raw, _CATALOG))
        self.assertEqual(_codes(raw, _CATALOG), set(),
                         "裕量提醒不该拦住生成")

    def test_missing_operating_data_is_recorded_not_passed(self):
        """目录没给推荐工作范围 = 没核对过，不是"通过"。"""
        parts = _reg(operating=(), supported_topologies=())
        rir = resolve(from_dict(_raw()), parts)
        self.assertTrue(rir.ok, "缺数据只能记「未核对」，不能直接判失败")
        self.assertNotIn("E-IR-ELECT-004", {d.code for d in rir.diagnostics})
        joined = " | ".join(rir.unverified)
        self.assertIn("U1", joined, "稳压器的输入/输出范围没核对，必须记下来")
        self.assertIn("拓扑", joined)

    def test_unknown_rail_capacitor_is_listed_unverified(self):
        """C2 在 VOUT 上，而 VOUT 没有端口声明 → 电压不可知，记 unverified。"""
        rir = resolve(from_dict(_raw()), _CATALOG)
        self.assertTrue(rir.ok, "不可知不等于有罪，不能因此报错")
        self.assertEqual([u.split(":")[0] for u in rir.unverified], ["C2"],
                         "不可知必须显式记下来，不能静默当通过")


class TestValueMatchesSelectedPart(unittest.TestCase):
    def test_value_contradicting_the_part_is_rejected(self):
        raw = _raw()
        _comp(raw, "C1")["value"] = "10uF"        # 料是 1uF
        self.assertIn("E-IR-PARTS-015", _codes(raw, _CATALOG))
        self.assertIn("E-IR-PARTS-015", _refuses(raw, _CATALOG))

    def test_close_value_is_a_warning(self):
        raw = _raw()
        _comp(raw, "C1")["value"] = "1.02uF"      # 差 2%，同一个料的不同写法
        self.assertEqual(_all_codes(raw, _CATALOG) - CATALOG_LEVEL_CODES,
                         {"W-IR-PARTS-002"})
        self.assertTrue(validate(from_dict(raw), _CATALOG).ok,
                        "接近但不相同只应提醒，不该拦路")

    def test_nominal_is_checked_against_the_injected_catalog(self):
        """value 的对照表必须是**注入的**目录：同一份 IR 换目录就得换结论。"""
        raw = _raw()
        _comp(raw, "C1")["value"] = "10uF"
        other = {**_CATALOG, "AUDIT-CAP-1U": _cap(nominal=Param(
            "capacitance", 10e-6, "F"))}
        self.assertEqual(_codes(raw, other), set())


class TestEffectiveFootprint(unittest.TestCase):
    """IR 的 footprint 要么生效、要么被拒——不能"忽略后悄悄用默认值"。"""

    def test_allowed_override_is_effective_and_visible(self):
        raw = _raw()
        _comp(raw, "U1")["footprint"] = ALT_FOOTPRINT
        parts = _reg()                         # 允许集合含 ALT_FOOTPRINT
        rir = resolve(from_dict(raw), parts)
        self.assertTrue(rir.ok, [d.code for d in rir.errors])
        self.assertEqual(rir.footprint_of("U1"), ALT_FOOTPRINT)
        self.assertEqual(rir.component("U1").footprint_source, "override")
        self.assertIn(f'"{ALT_FOOTPRINT}"', render(from_dict(raw), parts),
                      "覆盖通过了校验，图纸里就必须是它")
        self.assertEqual(rir.footprint_of("C1"),
                         "Capacitor_SMD:C_0603_1608Metric",
                         "没写覆盖的器件用目录默认封装，来源标 catalog")

    def test_denied_override_is_rejected_not_ignored(self):
        raw = _raw()
        _comp(raw, "U1")["footprint"] = DENIED_FOOTPRINT
        parts = _reg()
        self.assertIn("E-IR-PARTS-014", _codes(raw, parts))
        self.assertIn("E-IR-PARTS-014", _refuses(raw, parts))
        # 即使有人绕过生成器直接解析，也必须看得出"覆盖没生效"
        rir = resolve(from_dict(raw), parts)
        self.assertEqual(rir.component("U1").footprint_source, "catalog")

    def test_override_honours_the_injected_catalog(self):
        """同一份 IR、同一个封装：目录允不允许，结论必须跟着目录走。"""
        raw = _raw()
        _comp(raw, "U1")["footprint"] = ALT_FOOTPRINT
        narrow = _reg(allowed_footprints=("Package_TO_SOT_SMD:SOT-23-3",))
        self.assertIn("E-IR-PARTS-014", _codes(raw, narrow),
                      "该目录的允许集合里没有这个封装")
        self.assertEqual(_codes(raw, _reg()), set(),
                         "允许它的目录里必须通过——不能拿内置表当答案")

    def test_example_uses_catalog_footprints(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        rir = resolve(from_dict(raw), PARTS)
        for c in rir.components:
            self.assertEqual(c.footprint, PARTS[c.part_name].footprint,
                             f"{c.ref} 的有效封装应等于目录默认值")
            self.assertEqual(c.footprint_source, "catalog")


class TestTopology(unittest.TestCase):
    def test_part_that_does_not_support_the_topology(self):
        parts = _reg(supported_topologies=("async_buck",))
        raw = _raw()
        self.assertIn("E-IR-ELECT-005", _codes(raw, parts))
        self.assertIn("E-IR-ELECT-005", _refuses(raw, parts))

    def test_unknown_topology_support_is_not_a_failure(self):
        parts = _reg(supported_topologies=())
        self.assertEqual(_codes(_raw(), parts), set(),
                         "目录没声明支持哪种拓扑 = 未知，不该判失败")

    def test_async_buck_without_diode_is_rejected(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["components"] = [c for c in raw["components"] if c["ref"] != "D1"]
        for n in raw["nets"]:
            n["pins"] = [p for p in n["pins"] if p["ref"] != "D1"]
        self.assertIn("E-IR-PARTS-007", _codes(raw, PARTS))
        self.assertIn("E-IR-PARTS-007", _refuses(raw, PARTS))

    def test_sync_buck_needs_no_freewheel_diode(self):
        """同步 buck 的低边开关在芯片内：同一份 IR 换成同步拓扑就不该报缺二极管。"""
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["topology"] = "sync_buck"
        raw["components"] = [c for c in raw["components"] if c["ref"] != "D1"]
        for n in raw["nets"]:
            n["pins"] = [p for p in n["pins"] if p["ref"] != "D1"]
        self.assertNotIn("E-IR-PARTS-007", _codes(raw, PARTS))
        self.assertIn("E-IR-ELECT-005", _codes(raw, PARTS),
                      "MP1584EN 是异步芯片，换拓扑说法不改变它不支持同步这一事实")

    def test_example_is_clean_and_carries_its_revision(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        rir = resolve(from_dict(raw), PARTS)
        self.assertTrue(rir.ok, [d.code for d in rir.errors])
        self.assertGreaterEqual(len(rir.catalog.revision), 8)
        self.assertEqual(len(rir.components), len(raw["components"]))

    def test_render_stays_deterministic_with_an_injected_catalog(self):
        """注入目录不能带来不确定性：同样输入两次渲染必须逐字节相同。"""
        raw = _raw()
        _comp(raw, "U1")["footprint"] = ALT_FOOTPRINT
        first = render(from_dict(raw), _reg())
        self.assertEqual(first, render(from_dict(raw), _reg()))
        self.assertIn(f'"{ALT_FOOTPRINT}"', first)
        self.assertNotIn(f'"{ALT_FOOTPRINT}"', render(from_dict(_raw()), _reg()),
                         "没写覆盖的那份不该出现覆盖封装")

    def test_mutations_do_not_leak_into_the_example(self):
        """幂等性：resolve 不修改 IR，也不缓存上一次的结论。"""
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        before = copy.deepcopy(raw)
        first = resolve(from_dict(raw), PARTS).unverified
        second = resolve(from_dict(raw), PARTS).unverified
        self.assertEqual(first, second)
        self.assertEqual(raw, before)


if __name__ == "__main__":
    unittest.main()
