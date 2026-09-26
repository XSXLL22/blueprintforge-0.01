"""连接解析、必接脚、显式 NC、供电来源与必需子图的回归测试（T03）。

这些用例对应 T00 复现出来的**假通过**：

    duplicate_pin_across_nets      一只脚挂两个网 → ok=true，后写的赢
    missing_required_vin           漏接 VIN      → ok=true，还自动补 no_connect
    async_buck_without_diode       缺续流二极管   → ok=true
    （M1 的 EN 直连 VIN / 自举接 GND / 缺 FREQ / 并联补偿 RC 同样全部通过）

测试的判据是**行为**，不是"函数存在"：多数用例断言"必须被拦下"，
并且断言生成器**拒绝产出**（render 抛错），而不只是校验器打印了一行。
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from backend.kicad.schematic import connections_of, render
from checks import supply as supply_check
from checks.connectivity import resolve
from ir.schema import PowerIR, from_dict
from ir.validate import validate
from parts.partsdb import PARTS, Part, Pin

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"


def _raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _ir(raw: dict) -> PowerIR:
    return from_dict(raw)


def codes(out) -> set[str]:
    return {d.code for d in out.errors}


def net_of(raw: dict, name: str) -> dict:
    for n in raw["nets"]:
        if n["name"] == name:
            return n
    raise KeyError(name)


def comp_of(raw: dict, ref: str) -> dict:
    for c in raw["components"]:
        if c["ref"] == ref:
            return c
    raise KeyError(ref)


class TestOnePhysicalOwner(unittest.TestCase):
    """一只脚只能属于一个网络；判定与书写顺序、引脚写法无关。"""

    def test_pin_claimed_by_two_nets_is_rejected(self):
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "GND"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-010", codes(out))
        with self.assertRaises(ValueError):
            render(_ir(raw))

    def test_alias_and_number_are_the_same_pin(self):
        """U1 的 9 号脚叫 EP：写 "EP" 与写 "9" 指的是同一只脚。"""
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "9"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-010", codes(out),
                      "功能名与物理号必须归一化后再判冲突")

    def test_same_pin_twice_in_one_net_is_rejected(self):
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "VIN"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-009", codes(out))

    def test_reordering_nets_does_not_change_the_verdict(self):
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "GND"})
        before = codes(validate(_ir(raw)))
        raw["nets"].reverse()
        raw["components"].reverse()
        after = codes(validate(_ir(raw)))
        self.assertEqual(before, after,
                         "颠倒书写顺序不能改变接受与否（后写的赢=缺陷）")
        self.assertIn("E-IR-PARTS-010", after)

    def test_resolver_reports_every_claim_not_just_the_loser(self):
        """冲突诊断要给出全部争用者，否则修不干净。"""
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "GND"})
        conn, _ = resolve(_ir(raw))
        diag = next(d for d in conn.diagnostics if d.code == "E-IR-PARTS-010")
        self.assertIn("VIN", diag.actual + diag.evidence + diag.message)
        self.assertIn("GND", diag.actual + diag.evidence + diag.message)
        self.assertNotIn(("U1", "5"), conn.pin_net,
                         "有冲突的引脚不能带着'赢家'混进索引")


class TestRequiredPinsAndNc(unittest.TestCase):
    """必接脚必须接；悬空只能是显式且被器件允许的。"""

    def test_missing_vin_is_rejected(self):
        raw = _raw()
        net_of(raw, "VIN")["pins"] = [
            p for p in net_of(raw, "VIN")["pins"] if p["ref"] != "U1"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-011", codes(out))
        with self.assertRaises(ValueError):
            render(_ir(raw))

    def test_required_pin_cannot_be_hidden_by_nc(self):
        """把漏接的 VIN 直接声明成 nc——器件资料不允许，必须拦下。"""
        raw = _raw()
        net_of(raw, "VIN")["pins"] = [
            p for p in net_of(raw, "VIN")["pins"] if p["ref"] != "U1"]
        comp_of(raw, "U1")["nc"] = ["VIN"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-012", codes(out))

    def test_nc_and_connection_at_once_is_rejected(self):
        raw = _raw()
        comp_of(raw, "U1")["nc"] = ["EN"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-013", codes(out),
                      "EN 既接了分压又声明 nc，是自相矛盾")
        with self.assertRaises(ValueError):
            render(_ir(raw))

    def test_optional_pin_may_float_when_declared(self):
        """EN 悬空是器件资料允许的（nc_condition 非空）——必须能通过。"""
        raw = _raw()
        net_of(raw, "EN")["pins"] = [
            p for p in net_of(raw, "EN")["pins"] if p["ref"] != "U1"]
        comp_of(raw, "U1")["nc"] = ["EN"]
        out = validate(_ir(raw))
        self.assertEqual(codes(out), set(), out.render())
        sch = render(_ir(raw))
        self.assertEqual(sch.count("(no_connect "), 1,
                         "声明过的悬空脚应当有一个 no_connect")

    def test_omitted_pin_never_becomes_nc_by_itself(self):
        """删掉一条网线的引脚，生成器不得自己补 no_connect 蒙混过去。"""
        raw = _raw()
        net_of(raw, "FREQ")["pins"] = [
            p for p in net_of(raw, "FREQ")["pins"] if p["ref"] != "U1"]
        with self.assertRaises(ValueError) as ctx:
            render(_ir(raw))
        self.assertIn("U1.6", str(ctx.exception))

    def test_connectivity_index_excludes_nc_from_nets(self):
        raw = _raw()
        net_of(raw, "EN")["pins"] = [
            p for p in net_of(raw, "EN")["pins"] if p["ref"] != "U1"]
        comp_of(raw, "U1")["nc"] = ["EN"]
        conn, _ = resolve(_ir(raw))
        self.assertTrue(conn.is_nc("U1", "2"))
        self.assertIsNone(conn.net_of("U1", "2"))


class TestSupplyProvenance(unittest.TestCase):
    """电源网必须有可证明的来源；netclass 不是证据。"""

    def test_netclass_power_alone_is_not_a_driver(self):
        raw = _raw()
        raw["ports"] = [p for p in raw["ports"] if p["net"] != "VIN"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-005", codes(out))
        with self.assertRaises(ValueError):
            render(_ir(raw))

    def test_removing_netclass_does_not_dodge_the_check(self):
        """把 VIN 的 netclass 改成 signal 也躲不掉：它有 power_in 引脚。"""
        raw = _raw()
        raw["ports"] = [p for p in raw["ports"] if p["net"] != "VIN"]
        net_of(raw, "VIN").pop("netclass")
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-005", codes(out))

    def test_series_inductor_supply_is_proven(self):
        """VOUT 由 SW 经 L1 引来——不能因为没有直接 power_out 就误报。"""
        raw = _raw()
        self.assertTrue(validate(_ir(raw)).ok,
                        "示例本身必须是通过的正例")
        conn, _ = resolve(_ir(raw))
        supply, diags = supply_check.prove(_ir(raw), PARTS, conn)
        self.assertEqual([d for d in diags if d.code == "E-IR-NETS-005"], [])
        self.assertIn("L1", supply.proof["VOUT"])
        self.assertIn("SW", supply.proof["VOUT"])
        self.assertIn("VOUT", supply.flagged)
        self.assertNotIn("SW", supply.flagged,
                         "SW 有 power_out 引脚，不需要 PWR_FLAG")

    def test_breaking_the_series_path_removes_the_proof(self):
        """把电感从 SW–VOUT 上摘掉，VOUT 就失去来源——证明不是靠网名猜的。"""
        raw = _raw()
        net_of(raw, "SW")["pins"] = [
            p for p in net_of(raw, "SW")["pins"] if p["ref"] != "L1"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-005", codes(out))

    def test_port_on_unknown_net_is_rejected(self):
        raw = _raw()
        raw["ports"].append({"name": "STRAY", "direction": "input",
                             "net": "没有这个网", "drives": True})
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-006", codes(out))

    def test_duplicate_port_names_are_rejected(self):
        raw = _raw()
        raw["ports"].append({"name": "VIN_IN", "direction": "input",
                             "net": "VIN", "drives": True})
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-006", codes(out))

    def test_generator_flags_only_proven_nets(self):
        from backend.kicad.schematic import _flagged_power_nets
        raw = _raw()
        self.assertEqual(_flagged_power_nets(_ir(raw), PARTS),
                         {"VIN", "VOUT", "GND"})


class TestSubgraphTemplates(unittest.TestCase):
    """T01 顺延的四个反例子图：M1 时它们全部能通过检查。"""

    def test_bootstrap_cap_must_bridge_bst_and_sw(self):
        """自举电容接在 BST–GND（M1 的 H3）。"""
        raw = _raw()
        net_of(raw, "BST")["pins"] = [
            p for p in net_of(raw, "BST")["pins"] if p["ref"] != "C4"]
        net_of(raw, "GND")["pins"].append({"ref": "C4", "pin": "1"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-008", codes(out))
        self.assertIn("BST", out.render())

    def test_en_must_not_be_tied_to_vin(self):
        """EN 直接接 VIN（M1 的 H2）：13.2V 超过该脚 6V 绝对最大值。"""
        raw = _raw()
        net_of(raw, "EN")["pins"] = [
            p for p in net_of(raw, "EN")["pins"] if p["ref"] != "U1"]
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "EN"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-ELECT-004", codes(out))
        self.assertIn("6V", out.render())

    def test_freq_needs_resistor_to_ground(self):
        """FREQ 没有对地电阻（M1 没有这条支路）。

        为了只暴露子图缺陷，把 R4 改接到 VIN（电路仍然完整），
        于是 FREQ 上只剩芯片自己的脚。
        """
        raw = _raw()
        net_of(raw, "FREQ")["pins"] = [
            p for p in net_of(raw, "FREQ")["pins"] if p["ref"] != "R4"]
        net_of(raw, "VIN")["pins"].append({"ref": "R4", "pin": "1"})
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-008", codes(out))

    def test_comp_needs_series_rc_not_parallel(self):
        """补偿支路写成并联 RC（M1 的 H4）：R3 与 C3 都直接对地。"""
        raw = _raw()
        # R3.2 / C3.1 从 COMP_RC 挪到 GND → COMP 上只有并联的 R 与 C
        net_of(raw, "COMP_RC")["pins"] = []
        net_of(raw, "GND")["pins"] += [{"ref": "R3", "pin": "2"},
                                       {"ref": "C3", "pin": "1"}]
        out = validate(_ir(raw))
        self.assertIn("E-IR-NETS-008", codes(out))

    def test_series_rc_is_accepted(self):
        self.assertEqual(codes(validate(_ir(_raw()))), set(),
                         "示例的 COMP–R3–C3–GND 必须被接受")

    def test_rules_skipped_when_graph_is_broken(self):
        """连接有冲突时先修冲突，不叠加子图噪声。"""
        raw = _raw()
        net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "GND"})
        # 同时破坏自举子图：BST–SW 之间没有电容了
        net_of(raw, "BST")["pins"] = [
            p for p in net_of(raw, "BST")["pins"] if p["ref"] != "C4"]
        out = validate(_ir(raw))
        self.assertIn("E-IR-PARTS-010", codes(out))
        self.assertNotIn("E-IR-NETS-008", codes(out))

    def test_subgraph_violations_also_block_generation(self):
        """子图违规不能只是"打印一行"——生成器必须拒绝产出。

        T03 的初版只把连接冲突与供电无来源接进了生成器的预检，于是
        EN 直连 12V 这类子图违规仍会画出一张图：诊断说有错、产物却存在。
        这条回归测试把「生成器拒绝生成的那份判定 == 校验器报出来的那份」
        钉死，新增规则不会漏接。
        """
        def en_to_vin(raw):
            net_of(raw, "EN")["pins"] = [
                p for p in net_of(raw, "EN")["pins"] if p["ref"] != "U1"]
            net_of(raw, "VIN")["pins"].append({"ref": "U1", "pin": "EN"})

        def bst_to_gnd(raw):
            net_of(raw, "BST")["pins"] = [
                p for p in net_of(raw, "BST")["pins"] if p["ref"] != "C4"]
            net_of(raw, "GND")["pins"].append({"ref": "C4", "pin": "1"})

        def freq_no_resistor(raw):
            net_of(raw, "FREQ")["pins"] = [
                p for p in net_of(raw, "FREQ")["pins"] if p["ref"] != "R4"]
            net_of(raw, "VIN")["pins"].append({"ref": "R4", "pin": "1"})

        def comp_parallel(raw):
            net_of(raw, "COMP_RC")["pins"] = []
            net_of(raw, "GND")["pins"] += [{"ref": "R3", "pin": "2"},
                                           {"ref": "C3", "pin": "1"}]

        for name, mutate, expected in (
                ("EN→VIN", en_to_vin, "E-IR-ELECT-004"),
                ("BST→GND", bst_to_gnd, "E-IR-NETS-008"),
                ("缺 FREQ 电阻", freq_no_resistor, "E-IR-NETS-008"),
                ("并联 COMP RC", comp_parallel, "E-IR-NETS-008")):
            with self.subTest(name):
                raw = _raw()
                mutate(raw)
                self.assertIn(expected, codes(validate(_ir(raw))))
                with self.assertRaises(ValueError) as ctx:
                    render(_ir(raw))
                self.assertIn(expected, str(ctx.exception))


class TestCatalogInjection(unittest.TestCase):
    """目录注入必须一路传到连接解析（T00: injected_parts_disconnected）。"""

    def _custom(self, **pin_changes) -> dict[str, Part]:
        reg = PARTS["MP1584EN"]
        pins = tuple(
            Pin(p.number, pin_changes.get(p.number, p.name), p.kind,
                p.required, p.nc_condition)
            for p in reg.pins)
        return {**PARTS, "MP1584EN": Part(**{**reg.__dict__, "pins": pins})}

    def test_injected_pin_names_are_what_get_resolved(self):
        raw = _raw()
        parts = self._custom(**{"6": "FREQ_SET"})   # IR 里写的是 "FREQ"
        conn, diags = resolve(_ir(raw), parts)
        self.assertIn("E-IR-PARTS-009", {d.code for d in diags})
        self.assertTrue(resolve(_ir(raw), PARTS)[0].ok,
                        "默认目录下同一份 IR 必须解析通过")

    def test_connection_index_follows_the_injected_catalog(self):
        """注入目录后 U1.3 不再叫 COMP，连接索引必须跟着目录走。"""
        raw = _raw()
        parts = self._custom(**{"3": "COMP_X"})
        conn, _ = resolve(_ir(raw), parts)
        self.assertNotIn(("U1", "3"), conn.pin_net,
                         "IR 写 'COMP' 时不能靠全局目录把它接到 U1.3")

    def test_generation_uses_injected_catalog_end_to_end(self):
        """一只只存在于注入目录里的器件必须能正常生成（不回落全局表）。"""
        # 这份 fixture 是「稳压器 + 输入电容」，就是 LDO 电路；原先写
        # async_buck 是错的（异步 buck 必须有电感和续流二极管，IR 里没有，
        # E-IR-PARTS-007 会正确地拒掉）。本测试要证的是目录注入贯通，不该
        # 靠一份拓扑上不成立的电路来证，故按它实际是什么拓扑来声明。
        raw = {
            "schema_version": "0.2", "project": "inj", "module_type": "power.ldo",
            "topology": "ldo",
            "electrical": {"vin": {"min": 4.5, "typ": 5.0, "max": 5.5},
                           "vout": {"min": 3.2, "typ": 3.3, "max": 3.4},
                           "iout_max": 0.5},
            "components": [
                {"ref": "U1", "part": "MYREG", "role": "regulator"},
                {"ref": "C1", "part": "MYCAP", "role": "capacitor",
                 "value": "1uF"},
            ],
            "nets": [
                {"name": "VIN", "netclass": "power", "pins": [
                    {"ref": "U1", "pin": "VIN"}, {"ref": "C1", "pin": "1"}]},
                {"name": "VOUT", "netclass": "power", "pins": [
                    {"ref": "U1", "pin": "VOUT"}]},
                {"name": "GND", "netclass": "power", "pins": [
                    {"ref": "U1", "pin": "GND"}, {"ref": "C1", "pin": "2"}]},
            ],
            "targets": {},
            "ports": [
                {"name": "VIN_IN", "direction": "input", "net": "VIN",
                 "drives": True},
                {"name": "GND_REF", "direction": "bidirectional", "net": "GND",
                 "drives": True},
            ],
            "assumptions": ["x"], "risks": ["y"], "design_rationale": "z",
        }
        custom = {
            "MYREG": Part(name="MYREG", category="regulator", package="X",
                          spec="自定义稳压器", lifecycle="active",
                          pins=(Pin("1", "VIN", "power_in"),
                                Pin("2", "VOUT", "power_out"),
                                Pin("3", "GND", "power_in"))),
            "MYCAP": Part(name="MYCAP", category="capacitor", package="0402",
                          spec="自定义电容", lifecycle="active",
                          pins=(Pin("1", "1", "passive"),
                                Pin("2", "2", "passive"))),
        }
        ir = _ir(raw)
        conn = connections_of(ir, custom)
        # 自定义稳压器的**每一只脚**都要按注入目录接上：脚名对不上就接不上，
        # 只查一只脚会让"其余脚悄悄没接"漏过去。
        self.assertEqual({p: conn.net_of("U1", p) for p in ("1", "2", "3")},
                         {"1": "VIN", "2": "VOUT", "3": "GND"})
        self.assertEqual({p: conn.net_of("C1", p) for p in ("1", "2")},
                         {"1": "VIN", "2": "GND"})
        sch = render(ir, custom)
        # 电源网按本工程约定画成电源符号（power netclass → PWR_*），不是 label；
        # 这里要证的是**注入目录**里的器件造出了这张图，且没有回落全局表。
        self.assertIn('(symbol "circuitos:MYREG"', sch)
        self.assertIn('(lib_id "circuitos:PWR_VIN")', sch)
        self.assertNotIn("MP1584EN", sch,
                         "注入目录生效时绝不能回落到全局器件库")


if __name__ == "__main__":
    unittest.main()
