"""导出网表的解析与逐引脚双向对账（T05）。

判据是**行为**：对账必须在"图与 IR 不一致"时给出稳定错误码和受影响的
ref/pin/net，并且在真实正确的导出上**一条不报**（假阳性同样致命——
它会让人学会忽略这个环节）。

真实导出用 kicad-cli（`--format kicadxml`）现跑，不引用历史产物；
无 CLI 的机器上跳过那一组，其余用与真实结构一致的手写 XML。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from backend.kicad.cli import KicadError, export_netlist, find_cli
from backend.kicad.netlist import (NetlistError, auxiliary_refs,
                                   normalize_net_name, parse_kicadxml,
                                   read_netlist, reconcile_resolved)
from backend.kicad.project import write_project
from backend.kicad.schematic import write_schematic
from ir.schema import from_dict
from ir.resolved import resolve
from parts.partsdb import PARTS

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"

#: 与真实 kicadxml 结构一致的最小样例（手写，两网络三器件）。
MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <design>
    <source>minimal.kicad_sch</source>
    <date>2026-01-01T00:00:00</date>
    <tool>Eeschema 10.0.6</tool>
    <sheet number="1" name="/" tstamps="/">
      <title_block><title>t</title></title_block>
    </sheet>
  </design>
  <components>
    <comp ref="U1"><value>MP1584EN</value><footprint>F</footprint></comp>
    <comp ref="L1"><value>10uH</value><footprint>F</footprint></comp>
    <comp ref="C1"><value>22uF</value><footprint>F</footprint></comp>
  </components>
  <nets>
    <net code="1" name="/SW" class="Default">
      <node ref="U1" pin="1" pinfunction="SW" pintype="power_out"/>
      <node ref="L1" pin="1" pinfunction="1" pintype="passive"/>
    </net>
    <net code="2" name="/VOUT" class="Default">
      <node ref="L1" pin="2" pinfunction="2" pintype="passive"/>
      <node ref="C1" pin="1" pinfunction="1" pintype="passive"/>
    </net>
    <net code="3" name="GND" class="Default">
      <node ref="C1" pin="2" pinfunction="2" pintype="passive"/>
    </net>
  </nets>
</export>
"""


def _raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


# ---- 变异工具：对**真实导出**做结构改动 -------------------------------------

def _mutate(text: str, fn) -> str:
    """改 XML 树，并**断言变异真的发生了**。

    用字符串替换做变异有个隐蔽陷阱：真实 kicadxml 的 `<node>` 并不带
    `pinfunction` 属性，`replace('<node ref="R4" pin="1" pinfunction="1"…>')`
    一个字符都匹配不到，"删掉一只脚"其实什么都没删——测试照样绿，
    却什么都没验证。走 XML 树 + 断言树变了，才能让变异失败变得响亮。
    """
    root = ET.fromstring(text)
    before = ET.tostring(root)
    fn(root)
    after = ET.tostring(root)
    if before == after:
        raise AssertionError("变异没生效——这个测试会假通过")
    return after.decode("utf-8")


def _net(root: ET.Element, name: str) -> ET.Element:
    for n in root.findall("nets/net"):
        if n.get("name") == name:
            return n
    raise AssertionError(f"网表里没有网络 {name!r}")


def _drop_node(root: ET.Element, ref: str, pin: str) -> None:
    for net in root.findall("nets/net"):
        for node in list(net.findall("node")):
            if node.get("ref") == ref and node.get("pin") == pin:
                net.remove(node)
                return
    raise AssertionError(f"网表里没有 {ref}.{pin}")


def _add_node(root: ET.Element, net_name: str, ref: str, pin: str) -> None:
    ET.SubElement(_net(root, net_name), "node",
                  {"ref": ref, "pin": pin, "pintype": "passive"})


def _empty_nets(root: ET.Element) -> None:
    nets = root.find("nets")
    for n in list(nets):
        nets.remove(n)


class TestNetNameNormalization(unittest.TestCase):
    """归一化只做一件确定的事：剥掉根图纸局部标签的那一个前导斜杠。"""

    def test_root_label(self):
        self.assertEqual(normalize_net_name("/BST"), "BST")
        self.assertEqual(normalize_net_name("GND"), "GND")
        self.assertEqual(normalize_net_name("/COMP_RC"), "COMP_RC")

    def test_lstrip_would_have_collided(self):
        """`lstrip('/')` 会把这两个不同的网并成一个——本实现必须区分。"""
        self.assertEqual(normalize_net_name("//X"), "/X")
        self.assertNotEqual(normalize_net_name("//X"), normalize_net_name("X"))


class TestParse(unittest.TestCase):
    def test_minimal_document(self):
        net = parse_kicadxml(MINIMAL, source="minimal")
        self.assertEqual([n.name for n in net.nets], ["/SW", "/VOUT", "GND"])
        self.assertEqual(len(net.components), 3)
        self.assertEqual(net.tool, "Eeschema 10.0.6")

    def test_empty_text_is_not_zero_violations(self):
        for text in ("", "   \n"):
            with self.assertRaises(NetlistError):
                parse_kicadxml(text, source="empty")

    def test_truncated_document(self):
        with self.assertRaises(NetlistError) as ctx:
            parse_kicadxml(MINIMAL[: len(MINIMAL) // 2], source="trunc")
        self.assertIn("XML", str(ctx.exception))

    def test_missing_sections(self):
        broken = MINIMAL.replace("<nets>", "<notnets>").replace("</nets>",
                                                                "</notnets>")
        with self.assertRaises(NetlistError):
            parse_kicadxml(broken, source="broken")

    def test_no_nets_is_an_error(self):
        empty = re.sub(r"<nets>.*</nets>", "<nets></nets>", MINIMAL, flags=re.S)
        with self.assertRaises(NetlistError) as ctx:
            parse_kicadxml(empty, source="no-nets")
        self.assertIn("没有", str(ctx.exception))

    def test_duplicate_net_name_and_pin_are_counted_not_deduped(self):
        text = MINIMAL.replace('</nets>', """  <net code="4" name="/SW">
      <node ref="C1" pin="1" pinfunction="1" pintype="passive"/>
    </net>
  </nets>""")
        net = parse_kicadxml(text, source="dup")
        self.assertEqual(net.net_name_counts["/SW"], 2)
        diags = reconcile_resolved(resolve(from_dict(_raw()), PARTS), net)
        codes = {d.code for d in diags}
        self.assertIn("E-NET-003", codes, "同名网络出现两次必须报")
        self.assertIn("E-NET-004", codes, "C1.1 落在 /SW 而 IR 里在 VOUT")

    def test_repeated_node_is_counted(self):
        text = MINIMAL.replace('<node ref="L1" pin="1" pinfunction="1" pintype="passive"/>',
                               '<node ref="L1" pin="1" pintype="passive"/>\n'
                               '      <node ref="L1" pin="1" pintype="passive"/>')
        net = parse_kicadxml(text, source="dupnode")
        self.assertEqual(net.node_counts[("L1", "1")], 2)

    def test_hierarchical_sheet_is_rejected(self):
        text = MINIMAL.replace('name="/" tstamps="/"', 'name="/sub/" tstamps="/x/"')
        net = parse_kicadxml(text, source="hier")
        self.assertTrue(net.hierarchical)
        diags = reconcile_resolved(resolve(from_dict(_raw()), PARTS), net)
        self.assertIn("E-NET-006", {d.code for d in diags})

    def test_hierarchical_net_path_is_rejected(self):
        text = MINIMAL.replace('name="/SW"', 'name="/sub/SW"')
        net = parse_kicadxml(text, source="hiernet")
        self.assertIn("net /sub/SW", net.hierarchical)

    def test_read_netlist_reports_missing_file_as_diagnostic(self):
        parsed, diags = read_netlist(Path("no-such-dir/no-such-file.xml"))
        self.assertIsNone(parsed)
        self.assertEqual([d.code for d in diags], ["E-NET-001"])
        self.assertTrue(diags[0].is_error)

    def test_xml_entities_are_decoded(self):
        """XML 转义必须交给 XML 解析器：值里有 & < 也不能错位。"""
        text = MINIMAL.replace("<value>22uF</value>", "<value>A&amp;B&lt;C</value>")
        net = parse_kicadxml(text, source="entities")
        self.assertEqual(net.component("C1").value, "A&B<C")


@unittest.skipUnless(find_cli(), "需要 kicad-cli（装 KiCad 或设 COS_KICAD_CLI）")
class TestAgainstRealExport(unittest.TestCase):
    """真实导出上必须**一条不报**——和对账失败一样，假阳性也是缺陷。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name)
        ir = from_dict(_raw())
        sch = write_schematic(ir, out)
        write_project(sch)
        cls.sch = sch
        cls.xml = export_netlist(sch, out / "buck.xml").read_text(encoding="utf-8")
        cls.net = parse_kicadxml(cls.xml, source="real")
        cls.aux = auxiliary_refs(sch.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_real_export_has_expected_shape(self):
        self.assertEqual(len(self.net.components), 17)
        self.assertEqual(len(self.net.nets), 10)
        self.assertEqual(sorted(n.name for n in self.net.nets),
                         ["/BST", "/COMP", "/COMP_RC", "/EN", "/FB", "/FREQ",
                          "GND", "SW", "VIN", "VOUT"])

    def test_real_export_reconciles_clean(self):
        diags = reconcile_resolved(resolve(from_dict(_raw()), PARTS), self.net,
                                   aux_refs=self.aux)
        self.assertEqual([(d.code, d.message) for d in diags], [],
                         "真实正确的导出被误报，说明对账口径有错")

    def test_auxiliary_symbols_are_the_generated_ones(self):
        """辅助符号清单来自**本次生成的**原理图，且只含辅助符号。"""
        self.assertEqual(len(self.aux), 33)
        self.assertNotIn("U1", self.aux)
        self.assertNotIn("C1", self.aux)
        self.assertEqual(set(self.aux.values()),
                         {"power:GND", "power:PWR_FLAG", "circuitos:PWR_VIN",
                          "circuitos:PWR_VOUT", "circuitos:PWR_SW"})

    def test_power_symbol_refs_are_not_in_the_export(self):
        """KiCad 10.0.6 不把电源符号放进网表——但清单仍要能兜住它。"""
        self.assertEqual([c.ref for c in self.net.components
                          if c.ref.startswith("#")], [])

    # ---- 变异：每一种不一致都要有稳定错误码 -------------------------------

    def _codes(self, text: str) -> set[str]:
        net = parse_kicadxml(text, source="mutated")
        return {d.code for d in reconcile_resolved(
            resolve(from_dict(_raw()), PARTS), net, aux_refs=self.aux)}

    def test_missing_net(self):
        text = _mutate(self.xml, lambda r: r.find("nets").remove(_net(r, "/FREQ")))
        self.assertIn("E-NET-002", self._codes(text))

    def test_missing_pin(self):
        text = _mutate(self.xml, lambda r: _drop_node(r, "R4", "1"))
        self.assertIn("E-NET-003", self._codes(text))

    def test_wrong_net(self):
        """把 FB 的一个引脚搬到 GND 上：两个网名都合法，只是接错了。"""
        def tamper(root):
            _drop_node(root, "R2", "1")
            _add_node(root, "GND", "R2", "1")
        self.assertIn("E-NET-004", self._codes(_mutate(self.xml, tamper)))

    def test_duplicate_node_record(self):
        """同一只脚出现在两个网上：既要说"重复"，也要说各自属于哪个网。"""
        text = _mutate(self.xml, lambda r: _add_node(r, "GND", "R2", "1"))
        codes = self._codes(text)
        self.assertIn("E-NET-003", codes)
        self.assertIn("E-NET-004", codes)

    def test_extra_component(self):
        def tamper(root):
            comp = ET.SubElement(root.find("components"), "comp", {"ref": "X9"})
            ET.SubElement(comp, "value").text = "冒出来的器件"
        self.assertIn("E-NET-005", self._codes(_mutate(self.xml, tamper)))

    def test_unknown_pin_of_a_known_part(self):
        text = _mutate(self.xml, lambda r: _add_node(r, "GND", "C1", "7"))
        self.assertIn("E-NET-005", self._codes(text))

    def test_empty_nets_cannot_pass(self):
        with self.assertRaises(NetlistError):
            parse_kicadxml(_mutate(self.xml, _empty_nets), source="emptied")

    def test_auxiliary_symbol_in_the_export_is_excluded_by_id(self):
        """若某个 KiCad 版本把电源符号导出了，按生成的位号清单排除；
        清单之外的 `#...` 器件仍然要报。"""
        def tamper(root):
            comp = ET.SubElement(root.find("components"), "comp",
                                 {"ref": "#PWR001"})
            ET.SubElement(comp, "value").text = "GND"
        net = parse_kicadxml(_mutate(self.xml, tamper), source="withaux")
        codes = {d.code for d in reconcile_resolved(
            resolve(from_dict(_raw()), PARTS), net, aux_refs=self.aux)}
        self.assertEqual(codes, set(), "生成的辅助符号位号应被排除")
        codes2 = {d.code for d in reconcile_resolved(
            resolve(from_dict(_raw()), PARTS), net, aux_refs=())}
        self.assertEqual(codes2, {"E-NET-005"}, "不在清单里的就要报")


@unittest.skipUnless(find_cli(), "需要 kicad-cli（装 KiCad 或设 COS_KICAD_CLI）")
class TestProductionEntrypointRefusesMismatch(unittest.TestCase):
    """验收：对账不通过时，生产入口必须**非零退出**，不能报"全部通过"。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name)
        sch = write_schematic(from_dict(_raw()), out)
        write_project(sch)
        cls.good = out / "good.xml"
        cls.xml = export_netlist(sch, cls.good).read_text(encoding="utf-8")
        cls.out = out

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _run_gen(self, xml_text: str) -> int:
        """只替换导出的网表字节，**ERC 走真的**。

        T06 之前这里还顺手 patch 掉了 ERC——那样一来"入口拒绝不一致的网表"
        就只证明了入口的一半：ERC 那一段实际接线（`erc_mod.verify` 的返回值、
        `is_error` 的过滤）没被测到。现在 ERC 真跑，本类里出现退出码 2 就说明
        是 ERC 那道闸先拦下了（那是另一回事，不该混进对账的判据）。
        """
        import gen
        from pipeline import build as pipeline_build
        tampered = self.out / "tampered.xml"
        tampered.write_text(xml_text, encoding="utf-8")
        def export_current(sch, out):
            out.write_text(xml_text, encoding="utf-8")
            return out
        with patch.object(pipeline_build.cli, "export_netlist", side_effect=export_current):
            return gen.main([str(EXAMPLE), "-o", str(self.out / "build"), "--profile", "schematic"])

    def test_valid_netlist_exits_zero(self):
        self.assertEqual(self._run_gen(self.xml), 0)

    def test_missing_net_exits_nonzero(self):
        text = _mutate(self.xml, lambda r: r.find("nets").remove(_net(r, "/FREQ")))
        self.assertEqual(self._run_gen(text), 2)

    def test_wrong_net_exits_nonzero(self):
        def tamper(root):
            _drop_node(root, "R2", "1")
            _add_node(root, "GND", "R2", "1")
        self.assertEqual(self._run_gen(_mutate(self.xml, tamper)), 2)

    def test_empty_netlist_exits_nonzero(self):
        self.assertEqual(self._run_gen(_mutate(self.xml, _empty_nets)), 3)


@unittest.skipUnless(find_cli(), "需要 kicad-cli（装 KiCad 或设 COS_KICAD_CLI）")
class TestEntrypointToolFailure(unittest.TestCase):
    """工具出问题时，入口必须**报诊断 + 非零退出**，不能抛栈。

    T06 探针（`output/t06-evidence/t06_probes.py`）用一个"永远失败"的
    kicad-cli 替身跑生产入口，发现 `gen.py` 在 ERC 已经失败的情况下还是会去
    调 `export_netlist(..., check=True)`，于是 `KicadError` 冒到顶层 —— 一个
    未捕获的 traceback 顶替了本该是"核验未通过，退出码 2"的结论。
    异常类型对了、退出码丢了，「没验证」还可能被脚本当成「跑完了」。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_netlist_export_failure_exits_three(self):
        """网表导不出来 = 对账没做，谈不上"全部通过"。"""
        import gen
        from pipeline import build as pipeline_build
        boom = KicadError("kicad-cli sch export netlist 失败（退出码 1）")
        with patch.object(pipeline_build.cli, "export_netlist", side_effect=boom):
            code = gen.main([str(EXAMPLE), "-o", str(self.out / "a"), "--profile", "schematic"])
        self.assertEqual(code, 3, "工具失败必须是非零退出，不是抛栈")

    def test_erc_error_stops_before_netlist_export(self):
        """ERC 有 error 时不再导出网表：前提已不成立，做了只会掩盖问题。"""
        import gen
        from pipeline import build as pipeline_build
        from diagnostics import Diagnostic
        bad = [Diagnostic("E-ERC-002", "error", "sch", "ERC 没跑起来",
                          stage="erc", object_id="buck.kicad_sch")]
        with patch.object(pipeline_build.erc, "verify", return_value=(None, bad)), \
             patch.object(pipeline_build.cli, "export_netlist") as export:
            code = gen.main([str(EXAMPLE), "-o", str(self.out / "b"), "--profile", "schematic"])
        self.assertEqual(code, 3)
        export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
