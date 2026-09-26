"""(T06) ERC 适配器：逐违规诊断、报告对应性、登记制豁免。

判据（实施指南 T06 验收）：
- 空 items 的 error、未知严重级别、缺字段**都不能假通过**；
- 真实有效的报告（含合法零违规）**不能被误拒**；
- 未登记的告警不得自动进白名单；豁免必须写清规则、对象、理由、审查记录。

结构类用例用手写 JSON（字段与 KiCad 10.0.6 实测报告一致，见
`output/t06-evidence/`）；真实性用例现跑 kicad-cli，不引用历史产物。
工具边界（进程起不来/超时/非零退出）用 patch 模拟——那是**外部工具**，
适配器本身仍是真代码。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from backend.kicad import cli
from backend.kicad import erc as erc_mod
from backend.kicad.erc import (ErcReportError, Exemption, check_report,
                               diagnostics, load_exemptions, parse_report)
from backend.kicad.project import write_project
from backend.kicad.schematic import write_schematic
from ir.schema import load

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"
ALLOWLIST = ROOT / "backend" / "kicad" / "erc_allowlist.json"
FIXTURES = ROOT / "tests" / "fixtures" / "erc"


# ---- 构造器：字段与 KiCad 10.0.6 实测一致 -----------------------------------

def violation(kind="pin_not_connected", severity="error",
              description="Pin not connected", items=None) -> dict:
    if items is None:
        items = [{"description": "Symbol R1 引脚 1 [Passive, Line]",
                  "pos": {"x": 10.16, "y": 13.97}, "uuid": "aaaa-1111"}]
    return {"type": kind, "severity": severity, "description": description,
            "items": items}


def report(*, source="buck.kicad_sch", date="2026-09-26T12:00:00",
           kicad_version="10.0.6", severities=("error", "warning"),
           ignored=(), violations=()) -> str:
    """一份结构完整的最小报告（字段与 KiCad 10.0.6 实测一致）。"""
    data = {"$schema": "https://schemas.kicad.org/erc.v1.json",
            "coordinate_units": "mm", "date": date,
            "kicad_version": kicad_version,
            "included_severities": list(severities),
            "ignored_checks": [{"key": k, "description": f"{k} 说明"}
                               for k in ignored],
            "source": source,
            "sheets": [{"path": "/", "uuid_path": "/x",
                        "violations": list(violations)}]}
    return json.dumps(data, ensure_ascii=False)


def parsed(**kw):
    return parse_report(report(**kw), path="<test>")


def codes(diags) -> list[str]:
    return sorted(d.code for d in diags)


# ---- 结构不符：一律失败，不返回"零违规" --------------------------------------

class TestParseRejectsMalformed(unittest.TestCase):
    def test_empty_text(self):
        for text in ("", "   \n\t "):
            with self.assertRaises(ErcReportError):
                parse_report(text, path="<empty>")

    def test_corrupted_json(self):
        good = report(violations=[violation()])
        with self.assertRaises(ErcReportError) as ctx:
            parse_report(good[: len(good) // 2], path="<trunc>")
        self.assertIn("JSON", str(ctx.exception))

    def test_wrong_top_level(self):
        with self.assertRaises(ErcReportError) as ctx:
            parse_report("[]", path="<list>")
        self.assertIn("期望对象", str(ctx.exception))

    def test_missing_top_level_fields(self):
        for field in ("sheets", "kicad_version", "included_severities"):
            data = json.loads(report())
            data.pop(field)
            with self.subTest(field=field), self.assertRaises(ErcReportError):
                parse_report(json.dumps(data), path="<missing>")

    def test_sheet_without_violations_key(self):
        """"没有 violations 字段"不等于"没有违规"。"""
        data = json.loads(report())
        data["sheets"][0].pop("violations")
        with self.assertRaises(ErcReportError) as ctx:
            parse_report(json.dumps(data), path="<nosheetkey>")
        self.assertIn("violations", str(ctx.exception))

    def test_sheets_empty(self):
        data = json.loads(report())
        data["sheets"] = []
        with self.assertRaises(ErcReportError):
            parse_report(json.dumps(data), path="<nosheets>")

    def test_violation_missing_required_field(self):
        for field in ("type", "severity", "description", "items"):
            data = json.loads(report(violations=[violation()]))
            data["sheets"][0]["violations"][0].pop(field)
            with self.subTest(field=field), self.assertRaises(ErcReportError) as ctx:
                parse_report(json.dumps(data), path="<novfield>")
            self.assertIn(field, str(ctx.exception))

    def test_unknown_severity(self):
        for sev in ("info", "exclusion", "fatal", ""):
            with self.subTest(severity=sev), self.assertRaises(ErcReportError) as ctx:
                parsed(violations=[violation(severity=sev)])
            self.assertIn("严重级别", str(ctx.exception))

    def test_items_must_be_a_list(self):
        data = json.loads(report(violations=[violation()]))
        data["sheets"][0]["violations"][0]["items"] = {"uuid": "x"}
        with self.assertRaises(ErcReportError):
            parse_report(json.dumps(data), path="<items>")

    def test_ignored_checks_must_be_objects_with_key(self):
        data = json.loads(report())
        data["ignored_checks"] = ["single_global_label"]
        with self.assertRaises(ErcReportError):
            parse_report(json.dumps(data), path="<ignored>")


# ---- 逐违规语义 --------------------------------------------------------------

class TestViolationSemantics(unittest.TestCase):
    def test_empty_items_error_still_blocks(self):
        """空 items 的 error 必须**保留**并阻断——旧实现按 item 展开，
        这种违规一条都不报，正是"假通过"的来源。"""
        rep = parsed(violations=[violation(items=[])])
        self.assertEqual(len(rep.violations), 1)
        diags = diagnostics(rep)
        self.assertEqual(codes(diags), ["E-ERC-004"])
        self.assertIn("无对象信息", diags[0].message)

    def test_multiple_items_are_one_violation(self):
        rep = parsed(violations=[violation(items=[
            {"description": "A", "uuid": "u1"}, {"description": "B", "uuid": "u2"},
            {"description": "C", "uuid": "u3"}])])
        diags = diagnostics(rep)
        self.assertEqual(len(diags), 1, "3 个 items 不能变成 3 条违规")
        for name in ("A", "B", "C"):
            self.assertIn(name, diags[0].message)
        self.assertEqual(diags[0].rule_id, "pin_not_connected")
        self.assertEqual(diags[0].object_id,
                         "pin_not_connected#u1,u2,u3", "定位串要能看到全部对象")

    def test_warning_without_exemption_is_visible_not_whitelisted(self):
        rep = parsed(violations=[violation(severity="warning")])
        diags = diagnostics(rep)
        self.assertEqual(codes(diags), ["W-ERC-002"])
        self.assertFalse(diags[0].is_error, "未登记的告警不阻断，但必须露出来")

    def test_exempted_warning_carries_reason_and_review(self):
        item = {"description": "FB 分压网络", "uuid": "fb-1"}
        rep = parsed(violations=[violation(severity="warning", items=[item])])
        ex = Exemption(type="pin_not_connected", object_id="fb-1",
                       reason="分压网络由芯片内部环路驱动", reviewed="review/T06.md#fb")
        diags = diagnostics(rep, [ex])
        self.assertEqual(codes(diags), ["W-ERC-001"])
        self.assertIn("分压网络由芯片内部环路驱动", diags[0].message)
        self.assertIn("review/T06.md#fb", diags[0].message)

    def test_wildcard_exemption_matches_any_object(self):
        rep = parsed(violations=[violation(severity="warning", items=[
            {"description": "x", "uuid": "whatever"}])])
        ex = Exemption(type="pin_not_connected", object_id="*",
                       reason="r", reviewed="review/x.md")
        self.assertEqual(codes(diagnostics(rep, [ex])), ["W-ERC-001"])

    def test_exemption_does_not_match_other_objects_or_rules(self):
        rep = parsed(violations=[violation(severity="warning", items=[
            {"description": "x", "uuid": "other-uuid"}])])
        ex = Exemption(type="pin_not_connected", object_id="fb-1",
                       reason="r", reviewed="review/x.md")
        self.assertEqual(codes(diagnostics(rep, [ex])), ["W-ERC-002"])
        ex2 = replace(ex, type="power_pin_not_driven", object_id="*")
        self.assertEqual(codes(diagnostics(rep, [ex2])), ["W-ERC-002"])

    def test_error_is_never_exemptable(self):
        """error 级的违规，清单里登记了也必须阻断，而且要说明为什么没豁免。"""
        rep = parsed(violations=[violation(items=[
            {"description": "x", "uuid": "u1"}])])
        ex = Exemption(type="pin_not_connected", object_id="*",
                       reason="看起来没问题", reviewed="review/x.md")
        diags = diagnostics(rep, [ex])
        self.assertEqual(codes(diags), ["E-ERC-004"])
        self.assertIn("豁免只适用于 warning", diags[0].message)

    def test_zero_violations_is_clean(self):
        self.assertEqual(diagnostics(parsed()), [])


# ---- 豁免清单文件 ------------------------------------------------------------

class TestExemptionFile(unittest.TestCase):
    def _write(self, data) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "allow.json"
        path.write_text(data if isinstance(data, str)
                        else json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def test_missing_file_is_no_exemptions_not_an_error(self):
        exemptions, diags = load_exemptions(Path("no-such/allow.json"))
        self.assertEqual(exemptions, ())
        self.assertEqual(diags, [])

    def test_shipped_allowlist_is_valid_and_empty(self):
        exemptions, diags = load_exemptions(ALLOWLIST)
        self.assertEqual(diags, [])
        self.assertEqual(exemptions, (),
                         "示例当前不需要任何豁免；有豁免就必须写理由与审查记录")

    def test_broken_json(self):
        _, diags = load_exemptions(self._write("{oops"))
        self.assertEqual(codes(diags), ["E-ERC-005"])

    def test_wrong_shape(self):
        _, diags = load_exemptions(self._write({"exemptions": {}}))
        self.assertEqual(codes(diags), ["E-ERC-005"])

    def test_entry_missing_reason_or_review(self):
        for drop in ("reason", "reviewed", "type", "object_id"):
            entry = {"type": "pin_not_connected", "object_id": "*",
                     "reason": "r", "reviewed": "review/x.md"}
            entry.pop(drop)
            exemptions, diags = load_exemptions(
                self._write({"exemptions": [entry]}))
            with self.subTest(drop=drop):
                self.assertEqual(codes(diags), ["E-ERC-005"])
                self.assertEqual(exemptions, (), "非法条目不得生效")

    def test_entry_cannot_exempt_errors(self):
        exemptions, diags = load_exemptions(self._write({"exemptions": [
            {"type": "pin_not_connected", "object_id": "*", "severity": "error",
             "reason": "r", "reviewed": "review/x.md"}]}))
        self.assertEqual(codes(diags), ["E-ERC-005"])
        self.assertEqual(exemptions, ())


# ---- 报告与输入的对应关系 ----------------------------------------------------

class TestReportCorrespondence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.sch = self.dir / "buck.kicad_sch"
        self.sch.write_text("(kicad_sch)", encoding="utf-8")
        self.report_path = self.dir / "buck.erc.json"
        # 时间用固定基准，避免"报告时间 vs 当前挂钟"随测试运行时刻漂移
        self.now = time.time()
        os.utime(self.sch, (self.now - 10, self.now - 10))

    def _fresh(self, **kw) -> erc_mod.ErcReport:
        """一份与原理图**对应**的报告：源文件同名、版本一致、时间不早于它。"""
        kw.setdefault("date", datetime.fromtimestamp(self.now).strftime(
            "%Y-%m-%dT%H:%M:%S"))
        self.report_path.write_text(report(**kw), encoding="utf-8")
        os.utime(self.report_path, (self.now, self.now))
        return parse_report(self.report_path.read_text(encoding="utf-8"),
                            path=self.report_path)

    def test_matching_report_is_clean(self):
        self.assertEqual(check_report(self._fresh(), self.sch), [])

    def test_source_mismatch(self):
        diags = check_report(self._fresh(source="other.kicad_sch"), self.sch)
        self.assertIn("E-ERC-003", codes(diags))

    def test_report_without_error_severity_is_meaningless(self):
        diags = check_report(self._fresh(severities=("warning",)), self.sch)
        self.assertIn("E-ERC-003", codes(diags))

    def test_version_mismatch(self):
        diags = check_report(self._fresh(kicad_version="9.0.0"), self.sch,
                             cli_version_string="10.0.6")
        self.assertIn("E-ERC-003", codes(diags))

    def test_stale_report_file(self):
        rep = self._fresh()
        old = self.now - 120                     # 比原理图（now-10）还早 110s
        os.utime(self.report_path, (old, old))
        self.assertIn("E-ERC-003", codes(check_report(rep, self.sch)))

    def test_stale_report_date(self):
        diags = check_report(self._fresh(date="2020-01-01T00:00:00"), self.sch)
        self.assertIn("E-ERC-003", codes(diags))

    def test_ignored_important_check_warns(self):
        diags = check_report(self._fresh(ignored=("pin_not_connected",)),
                             self.sch)
        self.assertEqual(codes(diags), ["W-ERC-003"])
        self.assertFalse(diags[0].is_error)

    def test_ignored_unimportant_check_is_recorded_only(self):
        rep = self._fresh(ignored=("four_way_junction", "footprint_filter"))
        self.assertEqual(check_report(rep, self.sch), [])
        self.assertEqual(rep.ignored_keys,
                         ("four_way_junction", "footprint_filter"))


# ---- 工具边界：起不来 / 超时 / 非零退出 / 没有报告 ---------------------------

class TestToolBoundary(unittest.TestCase):
    """外部进程被替换，适配器逻辑是真的。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.sch = self.dir / "buck.kicad_sch"
        self.sch.write_text("(kicad_sch)", encoding="utf-8")
        self.report = self.dir / "buck.erc.json"

    def _proc(self, rc=0, out="", err=""):
        return subprocess.CompletedProcess([], rc, out, err)

    def test_cli_missing(self):
        with patch.object(cli, "find_cli", return_value=None):
            rep, diags = erc_mod.run(self.sch, self.report)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-002"])

    def test_nonzero_exit_keeps_summary(self):
        with patch.object(cli, "find_cli", return_value=Path("kicad-cli")), \
             patch.object(cli, "run", return_value=self._proc(1, "", "boom")):
            rep, diags = erc_mod.run(self.sch, self.report)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-002"])
        self.assertIn("boom", diags[0].message)

    def test_timeout(self):
        with patch.object(cli, "find_cli", return_value=Path("kicad-cli")), \
             patch.object(cli, "run",
                          side_effect=subprocess.TimeoutExpired("kicad-cli", 1)):
            rep, diags = erc_mod.run(self.sch, self.report, timeout=1)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-002"])
        self.assertIn("超时", diags[0].message)

    def test_rc_zero_without_report(self):
        with patch.object(cli, "find_cli", return_value=Path("kicad-cli")), \
             patch.object(cli, "run", return_value=self._proc(0, "ok")):
            rep, diags = erc_mod.run(self.sch, self.report)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-002"])
        self.assertIn("没有生成报告", diags[0].message)

    def test_stale_report_on_disk_is_deleted_before_running(self):
        """同名的旧报告必须先删掉：否则"本次结论"会是上一次的数据。"""
        self.report.write_text(report(violations=[violation()]), encoding="utf-8")
        with patch.object(cli, "find_cli", return_value=Path("kicad-cli")), \
             patch.object(cli, "run", return_value=self._proc(0, "ok")):
            rep, diags = erc_mod.run(self.sch, self.report)
        self.assertIsNone(rep, "旧报告被当成了本次结果")
        self.assertEqual(codes(diags), ["E-ERC-002"])

    def test_broken_report_is_a_report_error_not_a_tool_error(self):
        """报告坏了（E-ERC-001）和工具没跑起来（E-ERC-002）不是一回事。"""
        def fake_run(args, **kw):
            self.report.write_text('{"sheets": "nope"}', encoding="utf-8")
            return self._proc(0, "ok")
        with patch.object(cli, "find_cli", return_value=Path("kicad-cli")), \
             patch.object(cli, "run", side_effect=fake_run):
            rep, diags = erc_mod.run(self.sch, self.report)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-001"])

    def test_verify_returns_diagnostics_not_exceptions(self):
        with patch.object(cli, "find_cli", return_value=None):
            rep, diags = erc_mod.verify(self.sch, self.report)
        self.assertIsNone(rep)
        self.assertEqual(codes(diags), ["E-ERC-002"])


@unittest.skipUnless(cli.find_cli(), "需要 kicad-cli（装 KiCad 或设 COS_KICAD_CLI）")
class TestRealReports(unittest.TestCase):
    """真实报告：合法的零违规不得被误拒；有违规要逐条报出来。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.sch = write_schematic(load(EXAMPLE), cls.dir)
        write_project(cls.sch)
        cls.clean_report, cls.clean_diags = erc_mod.verify(
            cls.sch, cli.erc_report_path(cls.sch))
        # 拆掉一根导线，造一份**真的**有违规的报告（不是手写 JSON）
        text = cls.sch.read_text(encoding="utf-8")
        pat = re.compile(r"\t\(wire \(pts \(xy [^)]*\) \(xy [^)]*\)\)\n"
                         r"\t\t\(stroke[^\n]*\n\t\t\(uuid \"[^\"]*\"\)\)\n")
        match = pat.search(text)
        if match is None:
            raise AssertionError("生成的原理图里找不到 (wire ...)，无法构造违规样例")
        cls.broken = cls.dir / "broken.kicad_sch"
        cls.broken.write_text(text[:match.start()] + text[match.end():],
                              encoding="utf-8")
        shutil.copy(cls.sch.with_suffix(".kicad_pro"),
                    cls.broken.with_suffix(".kicad_pro"))
        cls.broken_report, cls.broken_diags = erc_mod.verify(
            cls.broken, cli.erc_report_path(cls.broken))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_clean_report_is_not_rejected(self):
        self.assertIsNotNone(self.clean_report, self.clean_diags)
        self.assertEqual(self.clean_diags, [], "合法零违规报告被误拒")
        self.assertEqual(self.clean_report.violations, ())

    def test_clean_report_exposes_ignored_checks(self):
        self.assertEqual(self.clean_report.ignored_keys,
                         ("single_global_label", "four_way_junction",
                          "simulation_model_issue", "footprint_filter"))
        self.assertTrue(all(desc for _, desc in
                            self.clean_report.ignored_checks))

    def test_broken_schematic_reports_every_violation(self):
        self.assertIsNotNone(self.broken_report, self.broken_diags)
        rep = self.broken_report
        self.assertGreater(len(rep.violations), 0)
        by_sev = Counter(v.severity for v in rep.violations)
        blocking = [d for d in self.broken_diags if d.code == "E-ERC-004"]
        warned = [d for d in self.broken_diags
                  if d.code in ("W-ERC-001", "W-ERC-002")]
        self.assertEqual(len(blocking), by_sev["error"],
                         "违规数 ≠ 诊断数（多 items 被拆开或漏报）")
        self.assertEqual(len(warned), by_sev["warning"])
        for d in blocking + warned:
            self.assertTrue(d.rule_id and d.object_id,
                            "诊断必须带规则 id 与对象定位")

    def test_broken_schematic_records_the_object_uuid(self):
        rep = self.broken_report
        self.assertTrue(all(v.uuids for v in rep.violations),
                        "真实报告的违规都带对象 uuid")

    def test_stale_copy_of_a_real_report_is_rejected(self):
        stale = self.dir / "stale.erc.json"
        shutil.copy(self.clean_report.path, stale)
        old = self.broken.stat().st_mtime - 3600
        os.utime(stale, (old, old))
        rep = parse_report(stale.read_text(encoding="utf-8"), path=stale)
        diags = check_report(rep, self.broken)
        self.assertIn("E-ERC-003", codes(diags))


class TestRealReportFixtures(unittest.TestCase):
    """kicad-cli 真实产出、落盘保存的报告（`tests/fixtures/erc/`）。

    为什么留这种 fixture：手写 JSON 只能证明"解析器同意我的想象"，真实报告
    才能证明"解析器同意工具的行为"。而且它们**不需要装 KiCad 就能跑**，
    所以"合法零违规报告不得被误拒"这条判据在任何机器上都被守着。

    出处、生成方法、指纹见 `tests/fixtures/erc/README.md` 与 `manifest.json`。
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        cls.entries = {e["file"]: e for e in cls.manifest["fixtures"]}
        cls.reports = {}
        for name in cls.entries:
            path = FIXTURES / name
            cls.reports[name] = parse_report(path.read_text(encoding="utf-8"),
                                             path=path)
        # 与真实报告同名的原理图，mtime 对齐到报告时间——对应性检查要过的
        # 是"这一版图"，不是"随便一份图"。用真原理图，别拿空文件糊弄。
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sch = write_schematic(load(EXAMPLE), Path(cls._tmp.name))
        ts = datetime.strptime(cls.reports["real-clean.json"].date,
                               "%Y-%m-%dT%H:%M:%S").timestamp()
        os.utime(cls.sch, (ts, ts))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def clean(self, name, **kw):
        return check_report(self.reports[name], self.sch,
                            cli_version_string="10.0.6", **kw)

    # ---- fixture 自身的可信度 ----
    def test_manifest_covers_every_file(self):
        on_disk = {p.name for p in FIXTURES.glob("*.json")
                   if p.name != "manifest.json"}
        self.assertEqual(on_disk, set(self.entries),
                         "fixture 与 manifest 对不上（多了或少了文件）")

    def test_fixture_bytes_match_recorded_hash(self):
        for name, entry in self.entries.items():
            digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
            self.assertEqual(digest, entry["sha256"],
                             f"{name} 与 manifest 记的 sha256 不符——"
                             f"换了 fixture 就要更新 manifest 与 README")

    def test_manifest_provenance_matches_the_bytes(self):
        """manifest 里记的每个字段都要能从报告本身读出来。

        这样 manifest 不是一份"声明"，而是**可证伪的记录**：报告改了而
        manifest 没改，这里立刻红。
        """
        for name, entry in self.entries.items():
            rep = self.reports[name]
            self.assertEqual(rep.kicad_version, entry["kicad_version"], name)
            self.assertEqual(rep.source, entry["source"], name)
            self.assertEqual(list(rep.included_severities),
                             entry["included_severities"], name)
            self.assertEqual(list(rep.ignored_keys), entry["ignored_checks"], name)
            self.assertEqual(len(rep.violations), entry["violations"], name)
            if "severity_counts" in entry:
                self.assertEqual(dict(Counter(v.severity for v in rep.violations)),
                                 entry["severity_counts"], name)

    # ---- 验收：合法零违规不得被误拒 ----
    def test_real_clean_report_is_not_rejected(self):
        rep = self.reports["real-clean.json"]
        self.assertEqual(rep.violations, ())
        self.assertEqual(diagnostics(rep), [], "真实零违规报告被误拒")
        self.assertEqual(self.clean("real-clean.json"), [],
                         "真实零违规报告在对应性检查上被误拒")

    def test_real_clean_report_still_shows_what_did_not_run(self):
        """零违规**不等于**全都查过：报告里未运行的检查必须读得出来。"""
        rep = self.reports["real-clean.json"]
        self.assertEqual(rep.ignored_keys,
                         ("single_global_label", "four_way_junction",
                          "simulation_model_issue", "footprint_filter"))
        # 这四项都不在 IMPORTANT_CHECKS 里，所以不告警——但也不能被藏起来
        self.assertEqual(codes(self.clean("real-clean.json")), [])

    # ---- 真实告警/错误 ----
    def test_real_warnings_do_not_enter_the_whitelist(self):
        rep = self.reports["real-warning-and-error.json"]
        diags = diagnostics(rep)          # 空豁免清单
        self.assertEqual(dict(Counter(d.code for d in diags)),
                         {"E-ERC-004": 1, "W-ERC-002": 2})
        for d in diags:
            if d.code == "W-ERC-002":
                self.assertTrue(d.rule_id and d.object_id,
                                "未登记告警必须指明是哪条规则的哪个对象")

    def test_real_warning_can_be_exempted_with_reason(self):
        rep = self.reports["real-warning-and-error.json"]
        warned = [v for v in rep.violations if not v.is_error]
        self.assertEqual(len(warned), 2, "fixture 应当有两条真实告警")
        target = warned[0]
        exemptions = (Exemption(type=target.type, object_id=target.uuids[0],
                                reason="本工程实测：符号岛被断，已确认不影响连接",
                                reviewed="docs/EXECUTION_LOG.md T06"),)
        diags = diagnostics(rep, exemptions)
        exempted = [d for d in diags if d.code == "W-ERC-001"]
        self.assertEqual(len(exempted), 1, "只豁免了命中的那一条")
        self.assertIn(exemptions[0].reason, exempted[0].message)
        self.assertIn(exemptions[0].reviewed, exempted[0].message)
        self.assertEqual(len([d for d in diags if d.code == "W-ERC-002"]), 1,
                         "同一规则的另一条对象没被覆盖，仍要露出")

    def test_real_error_is_never_exemptable(self):
        """真实 error + 试图豁免它：错误照报，且清单条目本身非法。"""
        rep = self.reports["real-warning-and-error.json"]
        err = next(v for v in rep.violations if v.is_error)
        path = Path(self._tmp.name) / "allowlist.json"
        path.write_text(json.dumps({"exemptions": [{
            "type": err.type, "object_id": err.uuids[0],
            "reason": "试图豁免一个 error", "reviewed": "无",
            "severity": "error"}]}, ensure_ascii=False), encoding="utf-8")
        exemptions, load_diags = load_exemptions(path)
        self.assertEqual(codes(load_diags), ["E-ERC-005"])
        self.assertEqual(exemptions, (), "非法条目不得生效")
        self.assertIn("E-ERC-004", codes(diagnostics(rep, exemptions)))

    # ---- 被禁用的重要检查 ----
    def test_real_ignored_important_check_is_surfaced(self):
        rep = self.reports["real-ignored-important.json"]
        diags = self.clean("real-ignored-important.json")
        ignored = [d for d in diags if d.code == "W-ERC-003"]
        self.assertEqual(len(ignored), 1,
                         "pin_not_connected 没跑，结论不覆盖它——必须告警")
        self.assertEqual(ignored[0].rule_id, "pin_not_connected")
        self.assertTrue(rep.violations == (), "这份 fixture 本身零违规")

    def test_real_ignored_report_is_still_accepted(self):
        """告警归告警：报告本身合法，不该因为多了个 ignored 就被拒。"""
        diags = self.clean("real-ignored-important.json")
        self.assertEqual([d for d in diags if d.is_error], [])

    def test_ignored_check_list_is_a_superset_of_the_clean_one(self):
        """两个 fixture 出自同一工程，只差一个被禁用的检查。"""
        clean = set(self.reports["real-clean.json"].ignored_keys)
        ignored = set(self.reports["real-ignored-important.json"].ignored_keys)
        self.assertEqual(ignored - clean, {"pin_not_connected"})

    # ---- 尺寸/结构的基本事实 ----
    def test_real_reports_carry_error_included_severities(self):
        for name, rep in self.reports.items():
            self.assertIn("error", rep.included_severities, name)


if __name__ == "__main__":
    unittest.main()
