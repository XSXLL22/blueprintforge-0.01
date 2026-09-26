"""IR 严格解码的回归测试（实施指南 T02 / §4.4）。

这一组用例的存在理由（对应审查报告 S7）：NaN、布尔、字符串冒充数值、
重复 key、拼错的约束键，过去都能一路走到生成工程文件——因为
NaN 参与的所有比较都是 False，于是"没有违规"。

因此每个错误用例都断言两件事：
  1. 对应的错误码出现；
  2. `draft is None`——**结构不合格就没有可用的 IR**，调用方无法继续。
第 2 条是关键：只报错但照样给出对象，等于把拦截变成了备注。

破坏点都落在示例 IR（已核对的正例）的副本上。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ir import decode, migrate
from ir.schema import SCHEMA_VERSION, from_dict, load, loads, to_dict
from ir.validate import validate

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"


def _raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


def _errors(result) -> set[str]:
    return {d.code for d in result.errors}


class DecodeCase(unittest.TestCase):
    """公共断言：结构错误 ⇒ 没有 draft；任何输入都不许抛异常。"""

    def assert_rejected(self, result, code: str):
        self.assertIn(code, _errors(result),
                      f"期望 {code}，实际 {sorted(_codes(result))}")
        self.assertFalse(result.usable,
                         "结构不合格时必须没有 draft——否则调用方会拿着"
                         "半成品继续生成工程文件")
        self.assertIsNone(result.draft)
        return result

    def assert_accepted(self, result):
        self.assertTrue(result.usable,
                        "\n".join(d.render() for d in result.diagnostics))
        self.assertIsNotNone(result.draft)
        return result


class TestStrongTyping(DecodeCase):
    """数值字段的类型/范围：布尔、字符串、NaN、Infinity、指数溢出。"""

    def test_string_where_number_expected(self):
        raw = _raw()
        raw["electrical"]["iout_max"] = "2.0"       # 字符串冒充数值
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_boolean_where_number_expected(self):
        raw = _raw()
        raw["electrical"]["iout_max"] = True        # bool 是 int 的子类
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")
        self.assertIn("布尔", result.errors[0].message)

    def test_bool_inside_range(self):
        raw = _raw()
        raw["electrical"]["vin"]["min"] = False
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_nan_literal(self):
        """NaN 必须是错误：它会让所有比较为假，等价于「没有违规」。"""
        text = json.dumps(_raw(), ensure_ascii=False).replace("10.8", "NaN", 1)
        result = self.assert_rejected(decode.decode_text(text), "E-IR-DECODE-004")
        self.assertIn("NaN", result.errors[0].message)
        self.assertEqual(result.errors[0].path, "electrical.vin.min")

    def test_infinity_literal(self):
        text = json.dumps(_raw(), ensure_ascii=False).replace(
            '"iout_max": 2.0', '"iout_max": Infinity')
        self.assertIn("Infinity", text)
        result = self.assert_rejected(decode.decode_text(text), "E-IR-DECODE-004")
        self.assertIn("Infinity", result.errors[0].message)

    def test_exponent_overflow(self):
        """1e400 在 JSON 里合法、在 float 里溢出成 inf——错误消息必须
        指回**字面量**，否则用户不知道改哪里。"""
        raw = _raw()
        text = json.dumps(raw, ensure_ascii=False).replace(
            '"iout_max": 2.0', '"iout_max": 1e400')
        self.assertIn("1e400", text)
        result = self.assert_rejected(decode.decode_text(text), "E-IR-DECODE-004")
        self.assertIn("1e400", result.errors[0].message)

    def test_target_string_value(self):
        raw = _raw()
        raw["targets"]["efficiency_min"] = "85%"    # 带单位字符串不算数值
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")
        self.assertIn("0.85", result.errors[0].message)   # 给出正确写法


class TestStructure(DecodeCase):
    def test_missing_required_field(self):
        raw = _raw()
        del raw["electrical"]["vout"]
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-002")

    def test_top_level_not_object(self):
        self.assert_rejected(decode.decode_ir([1, 2, 3]), "E-IR-DECODE-001")

    def test_unknown_field(self):
        raw = _raw()
        raw["efficency_target"] = ">85%"            # 拼错的目标键
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-005")
        self.assertIn("efficency_target", result.errors[0].message)

    def test_unknown_field_in_component(self):
        raw = _raw()
        raw["components"][0]["mpn"] = "MP1584EN"     # 目录之外的字段
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-005")

    def test_x_ext_is_the_escape_hatch(self):
        raw = _raw()
        raw["x_ext"] = {"notes": ["想放什么放什么"], "nested": {"a": 1}}
        raw["components"][0]["x_ext"] = {"todo": "确认封装"}
        self.assert_accepted(decode.decode_ir(raw))

    def test_duplicate_json_key(self):
        """同一个键写两次：取哪个值取决于解析器，必须消除歧义。"""
        text = json.dumps(_raw(), ensure_ascii=False).replace(
            '"module_type": "power.buck",',
            '"module_type": "power.buck", "module_type": "power.ldo",')
        result = self.assert_rejected(decode.decode_text(text), "E-IR-DECODE-006")
        self.assertIn("module_type", result.errors[0].message)

    def test_empty_component_list(self):
        raw = _raw()
        raw["components"] = []
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-008")

    def test_empty_net_list(self):
        raw = _raw()
        raw["nets"] = []
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-008")

    def test_wrong_container_type(self):
        raw = _raw()
        raw["nets"] = {"GND": ["U1"]}                # 对象冒充数组
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_unsupported_version(self):
        raw = _raw()
        raw["schema_version"] = "9.9"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-007")

    def test_unparseable_json(self):
        self.assert_rejected(decode.decode_text("{not json"), "E-IR-DECODE-010")

    def test_enum_value(self):
        raw = _raw()
        raw["nets"][0]["netclass"] = "powerful"      # 不在允许集合
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-013")

    def test_unknown_module_type(self):
        raw = _raw()
        raw["module_type"] = "power.boost"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-STRUCT-003")


class TestNamesAndReserved(DecodeCase):
    def test_project_name_type(self):
        raw = _raw()
        raw["project"] = 123                         # 旧版会抛 AttributeError
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_project_name_windows_reserved(self):
        """工程名会变成文件名：`con` 在 Windows 上建不出工程。"""
        raw = _raw()
        raw["project"] = "con"
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-STRUCT-002")
        self.assertIn("保留设备名", result.errors[0].message)

    def test_project_name_path_separator(self):
        raw = _raw()
        raw["project"] = "../escape"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-STRUCT-002")

    def test_project_name_leading_digit(self):
        raw = _raw()
        raw["project"] = "3v3_buck"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-STRUCT-002")

    def test_illegal_net_name(self):
        """网络名沿用语义层的码（E-IR-NETS-003），同一个缺陷只有一个码。"""
        raw = _raw()
        raw["nets"][0]["name"] = "VIN BUS"           # 含空格
        self.assert_rejected(decode.decode_ir(raw), "E-IR-NETS-003")

    def test_reserved_net_name(self):
        raw = _raw()
        raw["nets"][0]["name"] = "PWR_FLAG"          # 与 KiCad 电源标记同名
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-012")

    def test_reserved_ref(self):
        raw = _raw()
        raw["components"][0]["ref"] = "#PWR01"       # 电源符号的编号空间
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-012")

    def test_illegal_ref(self):
        raw = _raw()
        raw["components"][0]["ref"] = "U"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-009")

    def test_illegal_pin_token(self):
        raw = _raw()
        raw["nets"][0]["pins"][0]["pin"] = "1 2"     # 含空格
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-009")


class TestTargets(DecodeCase):
    def test_unknown_target_key(self):
        raw = _raw()
        raw["targets"]["ripple_max_mv"] = 50         # 键名带 mV，未登记
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")
        self.assertIn("ripple_max_v", result.errors[0].message)   # 提示正确键

    def test_ratio_over_one(self):
        raw = _raw()
        raw["targets"]["efficiency_min"] = 85        # 想写 85%，写成 85
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")
        self.assertIn("分数", result.errors[0].message)

    def test_plausibility_warning_is_not_blocking(self):
        """超出常理范围只警告：目的是抓单位错误，不是替设计者定指标。"""
        raw = _raw()
        raw["targets"]["ripple_max_v"] = 0.00000001  # 10nV 的纹波？多半是单位错
        result = self.assert_accepted(decode.decode_ir(raw))
        self.assertIn("W-IR-DECODE-001", _codes(result))

    def test_v2_target_with_conditions_roundtrip(self):
        ir = decode.decode_ir(_raw()).draft
        self.assertIsNotNone(ir)
        back = decode.decode_ir(to_dict(ir))
        self.assert_accepted(back)
        eff = back.draft.target("efficiency_min")
        self.assertIsNotNone(eff)
        self.assertEqual(eff.value, 0.85)
        self.assertEqual(eff.source, ">85%")
        self.assertEqual(eff.conditions["vin_v"], 12.0)
        self.assertEqual(eff.dimension, "ratio")


class TestPortsAndNc(DecodeCase):
    """v0.2 新增字段的结构校验（语义在 T03/T04 落地）。"""

    def test_ports(self):
        raw = _raw()
        raw["ports"] = [
            {"name": "VIN_IN", "direction": "input", "net": "VIN",
             "drives": True, "note": "J1 外部供电"},
            {"name": "VOUT_OUT", "direction": "output", "net": "VOUT"},
        ]
        result = self.assert_accepted(decode.decode_ir(raw))
        self.assertEqual([p.name for p in result.draft.ports],
                         ["VIN_IN", "VOUT_OUT"])
        self.assertTrue(result.draft.ports[0].drives)
        self.assertFalse(result.draft.ports[1].drives)

    def test_port_direction_enum(self):
        raw = _raw()
        raw["ports"] = [{"name": "VIN_IN", "direction": "sink", "net": "VIN"}]
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-013")

    def test_port_drives_flag_type(self):
        raw = _raw()
        raw["ports"] = [{"name": "VIN_IN", "net": "VIN", "drives": "yes"}]
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_port_roundtrip(self):
        raw = _raw()
        raw["ports"] = [{"name": "VIN_IN", "direction": "input", "net": "VIN",
                         "drives": True}]
        ir = decode.decode_ir(raw).draft
        self.assertEqual(decode.decode_ir(to_dict(ir)).draft.ports, ir.ports)

    def test_nc_list(self):
        raw = _raw()
        raw["components"][3]["nc"] = ["1"]
        self.assertEqual(decode.decode_ir(raw).draft.components[3].nc, ("1",))

    def test_nc_duplicate(self):
        raw = _raw()
        raw["components"][3]["nc"] = ["1", "1"]
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-008")


class TestMigration(DecodeCase):
    """v0.1 → v0.2：换算要留痕，看不懂的约束不能静默丢弃。"""

    def _v1(self) -> dict:
        raw = _raw()
        raw["schema_version"] = "0.1"
        raw["constraints"] = {
            "efficiency_target": ">85%",
            "ripple_target_mv": "<50",
            "bom_cost_target_cny": "<10",
        }
        del raw["targets"]
        return raw

    def test_constraints_become_typed_targets(self):
        result = self.assert_accepted(decode.decode_ir(self._v1()))
        ir = result.draft
        self.assertEqual(ir.schema_version, SCHEMA_VERSION)
        self.assertEqual(ir.target("efficiency_min").value, 0.85)
        # mV → V：键名带 mv，裸数字 50 就是 50mV
        self.assertEqual(ir.target("ripple_max_v").value, 0.05)
        self.assertEqual(ir.target("bom_cost_max_cny").value, 10.0)

    def test_source_text_is_kept(self):
        ir = decode.decode_ir(self._v1()).draft
        self.assertEqual(ir.target("ripple_max_v").source, "<50")
        self.assertEqual(ir.target("efficiency_min").source, ">85%")

    def test_conversion_record_and_warning(self):
        """迁移是人工复核入口，不是自动背书。"""
        result = self.assert_accepted(decode.decode_ir(self._v1()))
        self.assertTrue(result.migrations)
        self.assertTrue(any(">85%" in m for m in result.migrations))
        self.assertEqual(
            [d.code for d in result.warnings].count("W-IR-DECODE-002"), 3)

    def test_unit_in_text_is_not_multiplied_twice(self):
        raw = self._v1()
        raw["constraints"]["ripple_target_mv"] = "<50mV"     # 自带单位
        ir = decode.decode_ir(raw).draft
        self.assertEqual(ir.target("ripple_max_v").value, 0.05)

    def test_direction_mismatch(self):
        raw = self._v1()
        raw["constraints"]["efficiency_target"] = "<85%"     # 效率只有下限
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")
        self.assertIn("矛盾", result.errors[0].message)

    def test_percentage_without_sign(self):
        raw = self._v1()
        raw["constraints"]["efficiency_target"] = ">85"      # 少了 %
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")

    def test_unknown_legacy_key_is_not_dropped(self):
        raw = self._v1()
        raw["constraints"]["size_target_mm"] = "<30"
        result = self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")
        self.assertIn("size_target_mm", result.errors[0].message)

    def test_missing_direction(self):
        raw = self._v1()
        raw["constraints"]["ripple_target_mv"] = "50"
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-011")

    def test_non_string_constraint(self):
        raw = self._v1()
        raw["constraints"]["efficiency_target"] = 0.85
        self.assert_rejected(decode.decode_ir(raw), "E-IR-DECODE-003")

    def test_migration_is_idempotent(self):
        once = migrate.migrate_ir(self._v1(), [])[0]
        twice, notes = migrate.migrate_ir(once, [])
        self.assertEqual(once, twice)
        self.assertEqual(notes, [])

    def test_from_dict_uses_the_same_migration(self):
        """宽松入口与严格入口共用一条迁移路径，避免两套语义。"""
        ir = from_dict(self._v1())
        self.assertEqual(ir.schema_version, SCHEMA_VERSION)
        self.assertEqual(ir.target("efficiency_min").value, 0.85)


class TestNoExceptionLeaks(DecodeCase):
    """任何畸形输入都不许把 AttributeError/TypeError 抛给用户。"""

    NASTY = [
        None, 42, 3.5, True, "string", [], [[]], {"a": 1},
        {"schema_version": None, "project": None, "module_type": None,
         "topology": None, "electrical": None, "components": None,
         "nets": None},
        {"schema_version": "0.2", "project": "p", "module_type": "power.buck",
         "topology": "async_buck", "electrical": {"vin": {}, "vout": {},
                                                  "iout_max": None},
         "components": [None], "nets": [{"name": None, "pins": [None]}]},
    ]

    def test_no_exception(self):
        for value in self.NASTY:
            with self.subTest(value=repr(value)[:60]):
                result = decode.decode_ir(value)
                self.assertIsInstance(result, decode.DecodeResult)
                if not result.ok:
                    self.assertIsNone(result.draft)


class TestRealMigrationFile(unittest.TestCase):
    """用**真实的** v0.1 文件做迁移验收。

    `tests/fixtures/buck_12v_to_3v3_v01.json` 是 M1 提交（4942bf8）里的
    示例原文，原样保留（SHA-256 6245EFC77632EAA495317A9F0AD51DB19F94DD30A207F255F6A9F99ABB754B7）。
    它是"迁移前"的事实，不是为了让迁移好看而改写的输入——所以它仍然带着
    M1 的缺陷（D1 直连 EN、把不存在的 PGND 当引脚用），迁移不该掩盖它们。
    """

    V01 = ROOT / "tests" / "fixtures" / "buck_12v_to_3v3_v01.json"

    def test_v01_targets_match_the_live_v02_example(self):
        """手写的 v0.2 目标值必须与迁移换算结果一致（需求一字未改）。

        v0.1 的 `constraints` 只承载了三个目标（效率/纹波/成本）；输出精度
        当时藏在 `electrical.vout` 的 min/max 里，v0.2 才把它显式登记成
        `vout_accuracy`。所以两边的目标**集合**允许差这一项，值不能差。
        """
        v01 = decode.decode_text(self.V01.read_text(encoding="utf-8"))
        v02 = decode.decode_text(EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue(v01.usable, v01.render())
        self.assertTrue(v02.usable, v02.render())
        migrated = {t.key: t.value for t in v01.draft.targets}
        live = {t.key: t.value for t in v02.draft.targets}
        self.assertEqual(migrated, {"efficiency_min": 0.85,
                                    "ripple_max_v": 0.05,
                                    "bom_cost_max_cny": 10.0})
        self.assertEqual({k: live[k] for k in migrated}, migrated,
                         "迁移换算出来的值与手写的 v0.2 必须逐项一致")
        # 多出来的精度目标不能是凭空数字：它必须等于 electrical.vout 的口径
        vout = v02.draft.electrical.vout
        self.assertEqual(set(live) - set(migrated), {"vout_accuracy"})
        self.assertAlmostEqual(live["vout_accuracy"],
                               abs(vout.max - vout.typ) / vout.typ, places=9)
        self.assertEqual(v01.draft.target("ripple_max_v").value, 0.05)

    def test_migration_only_touches_the_expression(self):
        """迁移只换算表达方式：同一份文件，迁移前后电路内容必须逐字不变。"""
        raw = json.loads(self.V01.read_text(encoding="utf-8"))
        migrated = to_dict(decode.decode_text(
            self.V01.read_text(encoding="utf-8")).draft)
        lenient = to_dict(from_dict(raw))
        for key in ("project", "module_type", "topology", "electrical",
                    "components", "nets"):
            with self.subTest(field=key):
                self.assertEqual(migrated[key], lenient[key])

    def test_migrated_m1_file_still_shows_its_known_defects(self):
        """迁移不掩盖旧文件的缺陷：M1 版本里 U1 有凭空的 PGND 脚
        （器件目录里没有），而校验必须报出来，不能因为"迁移成功"就放行。

        T03 加入连接/供电检查后，这个文件还会多报三类**真实的**缺陷：
        FREQ 从未接线（M1 就没有这条支路）、VIN/GND 没有任何供电来源声明
        （M1 的 IR 没有 ports）。这些不是迁移引入的，迁移只换算表达方式
        ——`test_migration_only_touches_the_expression` 守住这一点。

        注意：M1 里 EN 直接接 VIN（H2）的反例规则（E-IR-ELECT-004）在这里
        **没有**出现——连接有冲突时子图规则不跑（图不可信时不叠加噪声），
        该反例由 T03 的专门用例覆盖。
        """
        ir = loads(self.V01.read_text(encoding="utf-8"))
        self.assertEqual(ir.schema_version, SCHEMA_VERSION)
        self.assertEqual({t.key for t in ir.targets},
                         {"efficiency_min", "ripple_max_v", "bom_cost_max_cny"})
        out = validate(ir)
        self.assertEqual(
            {d.code for d in out.errors},
            {"E-IR-PARTS-009",      # 凭空的 PGND 脚（H1 的残留）
             "E-IR-PARTS-011",      # U1.6 FREQ 从未接线
             "E-IR-NETS-005"},      # VIN / GND 没有可证明的供电来源
            out.render())
        self.assertNotIn("E-IR-ELECT-004", {d.code for d in out.errors},
                         "连接有冲突时不该再叠加子图诊断")


class TestDefenseInDepth(unittest.TestCase):
    """结构层是入口闸门，语义层仍要能自己发现非有限数。

    理由：`PowerIR` 也可以被直接构造（工具、测试、以后的其他前端）。
    NaN 一旦进来，"所有比较为假"的特性会让语义层的每条规则都失效——
    所以 validate 必须自己再查一次有限性，不能假设入口一定干净。
    """

    def test_validate_catches_nan_from_lenient_entry(self):
        raw = _raw()
        raw["electrical"]["iout_max"] = float("nan")
        out = validate(from_dict(raw))
        self.assertFalse(out.ok)
        self.assertIn("E-IR-DECODE-004", {d.code for d in out.errors})

    def test_validate_catches_nan_in_range(self):
        raw = _raw()
        raw["electrical"]["vin"] = {"min": float("nan"), "typ": 12.0, "max": 13.2}
        out = validate(from_dict(raw))
        self.assertIn("E-IR-DECODE-004", {d.code for d in out.errors})

    def test_validate_catches_infinity_current(self):
        raw = _raw()
        raw["electrical"]["iout_max"] = float("inf")
        out = validate(from_dict(raw))
        self.assertFalse(out.ok)
        self.assertIn("E-IR-DECODE-004", {d.code for d in out.errors})

    def test_lenient_entry_refuses_bool(self):
        """宽松入口也拒绝布尔冒充数值——bool 是 int 的子类，float(True)=1.0。"""
        raw = _raw()
        raw["electrical"]["iout_max"] = True
        with self.assertRaises(TypeError) as ctx:
            from_dict(raw)
        self.assertIn("布尔", str(ctx.exception))

    def test_lenient_entry_refuses_numeric_string(self):
        """`"2.0"` 也被拒绝：否则 "2A"/"2.0 " 的边界只能靠猜。"""
        raw = _raw()
        raw["electrical"]["iout_max"] = "2.0"
        with self.assertRaises(TypeError) as ctx:
            from_dict(raw)
        self.assertIn("不是数值", str(ctx.exception))

    def test_lenient_entry_reports_unknown_target_key(self):
        """拼错的目标键不能再抛裸 KeyError（那是内部表的实现细节）。"""
        raw = _raw()
        raw["targets"]["efficency_min"] = 0.85
        with self.assertRaises(ValueError) as ctx:
            from_dict(raw)
        self.assertIn("efficency_min", str(ctx.exception))
        self.assertIn("efficiency_min", str(ctx.exception))   # 给出正确键

    def test_validate_catches_ratio_over_one(self):
        """比值目标写着 85（想写 85%）——语义层也要拦。"""
        raw = _raw()
        raw["targets"]["efficiency_min"] = 85
        out = validate(from_dict(raw))
        self.assertFalse(out.ok)
        self.assertIn("E-IR-DECODE-011", {d.code for d in out.errors})

    def test_lenient_entry_does_not_drop_unreadable_constraint(self):
        """看不懂的 v0.1 约束不能被静默丢掉。

        丢掉一个约束的下游后果比报错更糟：目标列表少了一项，而"没有报错"
        会被读成"所有约束都满足"。所以迁移报错时 from_dict 必须抛。
        """
        raw = _raw()
        raw["schema_version"] = "0.1"
        raw["constraints"] = {"efficiency_target": ">85%",
                              "ripple_target_mv": "not-a-number"}
        with self.assertRaises(ValueError) as ctx:
            from_dict(raw)
        message = str(ctx.exception)
        self.assertIn("迁移失败", message)
        self.assertIn("E-IR-DECODE-011", message)
        self.assertIn("ripple_target_mv", message)


class TestEntryPoints(unittest.TestCase):
    def test_example_is_v02_and_decodes_clean(self):
        result = decode.decode_text(EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue(result.usable, result.render())
        self.assertEqual(result.errors, ())
        self.assertEqual(result.migrations, ())       # 已是 v0.2，无需迁移
        self.assertEqual(result.draft.schema_version, "0.2")

    def test_loads_raises_with_field_paths(self):
        text = json.dumps(_raw(), ensure_ascii=False).replace(
            '"iout_max": 2.0', '"iout_max": "2.0"')
        with self.assertRaises(ValueError) as ctx:
            loads(text)
        self.assertIn("E-IR-DECODE-003", str(ctx.exception))
        self.assertIn("electrical.iout_max", str(ctx.exception))

    def test_load_example_matches_decode(self):
        self.assertEqual(load(EXAMPLE).project, "buck_12v_to_3v3")

    def test_v01_load_is_accepted_and_migrated(self):
        raw = _raw()
        raw["schema_version"] = "0.1"
        raw["constraints"] = {"efficiency_target": ">85%"}
        del raw["targets"]
        ir = loads(json.dumps(raw, ensure_ascii=False))
        self.assertEqual(ir.schema_version, SCHEMA_VERSION)
        out = validate(ir)
        # 迁移后仍能全量校验：只有已登记的警告（NRFND + 成本目标缺条件），
        # 没有错误——迁移不会制造新的语义问题。
        # `W-CAT-001` 是目录层的口径声明（本机没有封装读取器时，每条带封装的
        # 记录各记一条），与这份 IR 的语义无关，故按码剔除后再比对；剔除是
        # 精确到码的，其他任何新警告仍然会让断言失败。
        self.assertTrue(out.ok, out.render())
        self.assertEqual({d.code for d in out.warnings} - {"W-CAT-001"},
                         {"W-IR-PARTS-001", "W-IR-DOC-004"})
