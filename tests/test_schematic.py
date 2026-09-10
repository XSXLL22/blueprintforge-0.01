"""原理图生成器测试。

- 纯 Python 部分：s-expression 结构、确定性、UUID 唯一、与 IR 的数量级一致；
- 装了 kicad-cli 才跑的集成部分（skipUnless）：ERC 0 错误 + 网表逐网络
  逐引脚与 IR 对账——连接关系由 KiCad 自己解析算出来的，最权威。
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kicad import symlib
from backend.kicad.cli import erc, export_netlist, find_cli
from backend.kicad.project import write_project
from backend.kicad.schematic import _pin_number, render, write_schematic
from ir.schema import load
from ir.validate import validate
from parts.partsdb import PARTS

IR = load(ROOT / "examples" / "buck_12v_to_3v3.json")


def balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def count(haystack: str, needle: str) -> int:
    return haystack.count(needle)


def parse_nets(text: str) -> dict[str, set[tuple[str, str]]]:
    """解析 KiCad .net 网表（括号计数提取 (nets) 块，node 内嵌套括号）。

    node 元素可能内嵌 (pinfunction ...) 等子元素，扁平正则抓不住——
    先用括号深度切出整个 (nets ...) 块，再按 (net 边界逐段解析。
    实现经 output/probe3.py 对真实网表验证过。
    """
    start = text.index("(nets")
    depth = 0
    block = ""
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                block = text[start:i + 1]
                break
    nets: dict[str, set[tuple[str, str]]] = {}
    for m in re.finditer(
            r'\(net\s*\(code "[^"]*"\)\s*\(name "([^"]+)"\)'
            r'(.*?)(?=\(net\s|\)\s*\Z)', block, re.S):
        # 本地标签网名带图纸路径前缀（/BST），电源全局网名不带（VIN/GND）
        name = m.group(1).lstrip("/")
        nets[name] = {(r, p) for r, p in re.findall(
            r'\(node\s*\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', m.group(2))}
    return nets


class TestStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sch = render(IR)

    def test_example_ir_validates_clean(self):
        outcome = validate(IR)
        self.assertEqual(outcome.errors, (), msg=str(outcome.errors))

    def test_balanced_parens(self):
        self.assertTrue(balanced(self.sch), "s-expression 括号必须配平")

    def test_version_and_generator_stamp(self):
        self.assertIn("(version 20250610)", self.sch)
        self.assertIn('(generator "circuitos")', self.sch)

    def test_deterministic(self):
        self.assertEqual(self.sch, render(IR), "同样输入必须逐字节相同")

    def test_uuid_unique(self):
        uuids = re.findall(r'\(uuid "([0-9a-f-]{36})"\)', self.sch)
        self.assertEqual(len(uuids), len(set(uuids)), "UUID 不得重复")

    def test_refs_unique_and_present(self):
        refs = re.findall(r'\(reference "([A-Z]+[0-9]+)"\)', self.sch)
        self.assertEqual(sorted(refs), sorted({c.ref for c in IR.components}),
                         "位号集合必须与 IR 一一对应且不重复")

    def test_grid_alignment(self):
        """我们放置的东西（导线/标签/实例/文字）都要落在 1.27 网格上。

        内嵌的官方符号定义（lib_symbols）是 vendor 进来的原文，坐标随
        官方库（如 1.905/0.635），不在本检查范围内。
        """
        start = self.sch.index("(lib_symbols")
        depth = 0
        end = start
        for i in range(start, len(self.sch)):
            if self.sch[i] == "(":
                depth += 1
            elif self.sch[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.sch[:start] + self.sch[end:]
        for x, y in re.findall(r"\(at (-?[\d.]+) (-?[\d.]+) 0\)", body):
            for v in (float(x), float(y)):
                self.assertAlmostEqual(v / 1.27, round(v / 1.27), places=6,
                                       msg=f"坐标 {v} 不在 1.27 网格上")

    def test_symbol_geometry_matches_pin_rows(self):
        """每个实例的 (pin "N" ...) 集合 == _pin_rows 给出的编号集合。"""
        from backend.kicad.schematic import _pin_rows
        for comp in IR.components:
            part = PARTS[comp.part]
            expected = [n for n, *_ in _pin_rows(part)]
            pos = self.sch.index(f'(reference "{comp.ref}")')
            sym_start = self.sch.rindex("\t(symbol", 0, pos)
            inst = self.sch.index("(instances", sym_start)
            got = re.findall(r'\(pin "([^"]+)" \(uuid',
                             self.sch[sym_start:inst])
            self.assertEqual(got, expected, f"{comp.ref} 的实例引脚表不符")

    def test_wire_label_power_counts(self):
        """导线=引脚连接+驱动岛；标签=信号引脚；电源符号各归其位。

        三种网络的连接机制（全部经 kicad-cli 实测）：
        - 信号网络：本地标签同名合并；
        - GND：官方 GND 符号按 Value 并入全局网；
        - 其它 power 网络：生成的 PWR_<net> 符号（power_in + (power global)）
          按 Value 自动全局合并——PWR_FLAG 做不到（引脚名空串，只能靠导线
          接触合并），多引脚电源网络会被拆散。
        - 无 power_out 引脚的网络：顶部 PWR_FLAG 岛标驱动。
        """
        from backend.kicad.schematic import _flagged_power_nets, _pwr_lib_id
        total_pins = sum(len(n.pins) for n in IR.nets)
        gnd_pins = sum(len(n.pins) for n in IR.nets if n.name == "GND")
        pwr_pins = sum(len(n.pins) for n in IR.nets
                       if n.name != "GND" and n.netclass == "power")
        signal_pins = total_pins - gnd_pins - pwr_pins
        flagged = _flagged_power_nets(IR, PARTS)
        islands = len(flagged)
        self.assertEqual(count(self.sch, "(wire "), total_pins + islands)
        self.assertEqual(count(self.sch, "(label "), signal_pins)
        self.assertEqual(count(self.sch, "(global_label "), 0)
        # GND 符号 = 每个 GND 引脚一个 + GND 岛一个
        self.assertEqual(count(self.sch, '(lib_id "power:GND")'),
                         gnd_pins + (1 if "GND" in flagged else 0))
        # PWR_FLAG 只在岛上（每个被标驱动的网络一个）
        self.assertEqual(count(self.sch, '(lib_id "power:PWR_FLAG")'), islands)
        # PWR_<net> 符号 = 每个非 GND 电源引脚一个 + 非 GND 岛一个
        expected_pwr_sym = pwr_pins + sum(1 for n in flagged if n != "GND")
        got_pwr_sym = sum(count(self.sch, f'(lib_id "{_pwr_lib_id(n.name)}")')
                          for n in IR.nets
                          if n.name != "GND" and n.netclass == "power")
        self.assertEqual(got_pwr_sym, expected_pwr_sym)
        self.assertEqual(count(self.sch, "(no_connect "), 0)

    def test_signal_nets_have_labels(self):
        for n in IR.nets:
            if n.netclass != "power":
                self.assertIn(f'(label "{n.name}"', self.sch,
                              f"信号网络 {n.name} 必须有标签")

    def test_official_symbols_embedded(self):
        """官方符号定义必须内嵌进 lib_symbols（kicad-cli 只认内嵌）。"""
        for lib_id in ("Device:R", "Device:C", "Device:L",
                       "Device:D_Schottky", "power:GND", "power:PWR_FLAG",
                       "Connector_Generic:Conn_01x02"):
            self.assertIn(f'(symbol "{lib_id}"', self.sch,
                          f"{lib_id} 没有内嵌进 lib_symbols")

    def test_generated_power_symbols_embedded(self):
        """每个非 GND 电源网络都有一个内嵌的 (power global) 符号定义。"""
        from backend.kicad.schematic import _pwr_lib_id
        for n in IR.nets:
            if n.name != "GND" and n.netclass == "power":
                self.assertIn(f'(symbol "{_pwr_lib_id(n.name)}"', self.sch)
                self.assertIn("(power global)", self.sch)

    def test_vendored_pin_geometry(self):
        """vendor 库解析出的引脚几何与官方定义一致（防漂移）。"""
        expect = {
            "Device:C": (("1", 0.0, 3.81, 270), ("2", 0.0, -3.81, 90)),
            "Device:D_Schottky": (("1", -3.81, 0.0, 0), ("2", 3.81, 0.0, 180)),
            "Connector_Generic:Conn_01x02": (("1", -5.08, 0.0, 0),
                                              ("2", -5.08, -2.54, 0)),
        }
        for lib_id, rows in expect.items():
            got = tuple((p.number, p.x, p.y, p.angle)
                        for p in symlib.pins_of(lib_id))
            self.assertEqual(got, rows, f"{lib_id} 引脚几何漂移")
        self.assertEqual(symlib.pins_of("power:GND")[0].kind, "power_in")
        self.assertEqual(symlib.pins_of("power:PWR_FLAG")[0].kind, "power_out")


@unittest.skipUnless(find_cli(), "需要 kicad-cli（装 KiCad 或设 COS_KICAD_CLI）")
class TestKicadCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name)
        cls.sch = write_schematic(IR, out)
        write_project(cls.sch)
        cls.errors, cls.warnings = erc(cls.sch)
        cls.net = export_netlist(cls.sch, out / f"{IR.project}.net")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_erc_no_errors(self):
        self.assertEqual(self.errors, [], msg="\n".join(self.errors))

    def test_erc_warnings_only_expected(self):
        # 允许的警告：FB（分压网络无输出驱动，KiCad 不理解芯片内部环路）
        allowed = ("Input pin not driven", "Input Power pin not driven")
        for w in self.warnings:
            self.assertTrue(any(a in w for a in allowed),
                            f"意外的 ERC 警告: {w}")

    def test_netlist_reconciliation(self):
        """KiCad 解析出来的网络连接必须与 IR 逐网络逐引脚一致。"""
        text = self.net.read_text(encoding="utf-8")
        netlist = parse_nets(text)
        self.assertTrue(netlist, "网表里没有解析出网络")
        for n in IR.nets:
            expected = set()
            for pr in n.pins:
                part = PARTS[IR.component_map[pr.ref].part]
                expected.add((pr.ref, _pin_number(part, pr.pin)))
            self.assertEqual(netlist.get(n.name), expected,
                             f"网络 {n.name} 与 IR 不一致")
        self.assertEqual(set(netlist), {n.name for n in IR.nets})

    def test_symbol_lib_and_table_written(self):
        """配套写 circuitos.kicad_sym + sym-lib-table（GUI 用，消库警告）。"""
        from backend.kicad.project import write_project
        out = Path(self._tmp.name)
        sch = write_schematic(IR, out)
        write_project(sch)
        lib = out / "circuitos.kicad_sym"
        table = out / "sym-lib-table"
        self.assertTrue(lib.is_file(), "circuitos.kicad_sym 未生成")
        self.assertTrue(table.is_file(), "sym-lib-table 未生成")
        self.assertIn('(symbol "circuitos:MP1584EN"', lib.read_text(encoding="utf-8"))
        self.assertIn('(symbol "circuitos:PWR_VIN"', lib.read_text(encoding="utf-8"))
        self.assertIn('"circuitos"', table.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
