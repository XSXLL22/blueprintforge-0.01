# T07 adaptation of historical T06 probes. Original evidence remains untouched.
"""T06 验收探针：ERC 适配器。

对照实施指南 T06 的验收原文：

    验收：mock 的空 items error、未知严重级别、缺字段均不能假通过；
    同时用真实有效报告 fixture 验证不会把合法零违规报告误拒绝。

探针分五节：

[1] 真实报告：合法零违规**不得被误拒**（真工具产出，落在 tests/fixtures/erc/）
[2] 真实报告：有违规必须逐条报出，error 挡住、warning 不自动进白名单
[3] 坏报告不得假通过：空 items error / 未知级别 / 缺字段 / 截断 / 顶层不对
[4] 生产入口的退出码：跑真的 `gen.main`（未经 patch 的代码路径）
[5] 被禁用的重要检查要露出来（W-ERC-003）

[4] 用的 kicad-cli 是一个**放进去的替身可执行文件**（COS_KICAD_CLI 指向
它）。替身是真实的进程，返回真实的退出码/写出真实的字节——所以"工具失败"
"报告不可用"这两条路径是真跑的，不是 mock；被替身换掉的只有 KiCad 本身，
`gen.py` 与 `erc.py` 都是生产代码。每处替身行为都在下面注明。

产物全部留在本目录：报告原文、运行记录、以及 [3] 的坏报告样本。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kicad import erc as erc_mod
from backend.kicad.cli import CLI_ENV, erc_report_path, find_cli
from backend.kicad.project import write_project
from backend.kicad.schematic import write_schematic
from ir.schema import load

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import uuid
from backend.kicad.footprint import footprints_root, FOOTPRINTS_ENV
HERE = ROOT / "output" / "t07-evidence" / ("t06-acceptance-" + uuid.uuid4().hex)
HERE.mkdir(parents=True)
real_footprints = footprints_root()
if real_footprints is not None:
    os.environ[FOOTPRINTS_ENV] = str(real_footprints)
FIXTURES = ROOT / "tests" / "fixtures" / "erc"
EXAMPLE = ROOT / "examples" / "buck_12v_to_3v3.json"
SCRATCH = HERE / "_scratch"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'通过' if ok else '**不通过**'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def codes(diags) -> list[str]:
    return [d.code for d in diags]


def codes_of(diags, sev) -> list[str]:
    return [d.code for d in diags if d.severity == sev]


# ---- [1][2] 真实报告 fixture ------------------------------------------------

def section_real() -> None:
    print("\n[1] 真实报告（kicad-cli 10.0.6 产出，见 tests/fixtures/erc/README.md）")
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    reports = {}
    for entry in manifest["fixtures"]:
        path = FIXTURES / entry["file"]
        reports[entry["file"]] = erc_mod.parse_report(
            path.read_text(encoding="utf-8"), path=path)
        rep = reports[entry["file"]]
        print(f"  {entry['file']}: {len(rep.violations)} 条违规"
              f"（{len(rep.errors)} 错 / {len(rep.warnings)} 警），"
              f"ignored={list(rep.ignored_keys)}")

    clean = reports["real-clean.json"]
    diags = erc_mod.diagnostics(clean)
    check("合法零违规报告不被误拒（逐违规诊断为空）", diags == [], str(codes(diags)))
    check("零违规报告的违规列表确实是空的", clean.violations == ())

    print("\n[2] 真实报告里的违规逐条报出")
    mixed = reports["real-warning-and-error.json"]
    diags = erc_mod.diagnostics(mixed)
    got = {c: codes(diags).count(c) for c in set(codes(diags))}
    print(f"  诊断分布（空豁免清单）：{got}")
    check("error 一律 E-ERC-004 且不可豁免", got.get("E-ERC-004") == len(mixed.errors),
          f"{got.get('E-ERC-004')} vs {len(mixed.errors)}")
    check("未登记的 warning 一律 W-ERC-002",
          got.get("W-ERC-002") == len(mixed.warnings),
          f"{got.get('W-ERC-002')} vs {len(mixed.warnings)}")
    check("每条诊断都带规则 id 与对象定位",
          all(d.rule_id and d.object_id for d in diags))

    warned = [v for v in mixed.warnings]
    target = warned[0]
    exemptions = (erc_mod.Exemption(
        type=target.type, object_id=target.uuids[0],
        reason="探针：验证登记制豁免通路", reviewed="output/t06-evidence/"),)
    diags2 = erc_mod.diagnostics(mixed, exemptions)
    check("登记的告警变成 W-ERC-001（带理由与审查记录）",
          codes(diags2).count("W-ERC-001") == 1,
          str({c: codes(diags2).count(c) for c in set(codes(diags2))}))
    w1 = next(d for d in diags2 if d.code == "W-ERC-001")
    check("W-ERC-001 的消息里能读到理由与审查记录",
          exemptions[0].reason in w1.message and exemptions[0].reviewed in w1.message)
    check("没登记的同类告警仍然露出来",
          codes(diags2).count("W-ERC-002") == 1,
          "同一规则的另一条对象没被一条豁免顺带放过")
    check("error 不因清单里写了就被放过",
          codes(diags2).count("E-ERC-004") == len(mixed.errors))


# ---- [3] 坏报告 -------------------------------------------------------------

def section_malformed() -> None:
    print("\n[3] 坏报告不得假通过（每份都落盘留证）")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    real = json.loads((FIXTURES / "real-warning-and-error.json")
                      .read_text(encoding="utf-8"))
    clean = json.loads((FIXTURES / "real-clean.json").read_text(encoding="utf-8"))

    def one_violation() -> dict:
        return json.loads(json.dumps(
            real["sheets"][0]["violations"][0]))     # 深拷贝一条真实违规

    def wrap(violation: dict) -> dict:
        """把一条（改坏的）违规包进**完整报告骨架**。

        必须包这一层：直接扔一条违规进去会被"缺顶层字段"先拦下——测试就变成
        在测别的东西，看着通过其实什么都没测（本探针第一版就栽在这，
        属于假通过）。所以下面每条还要求"拒绝理由"确实提到预期的缺陷。
        """
        rep = json.loads(json.dumps(clean))
        rep["sheets"] = [{"path": "Root", "uuid_path": "/",
                          "violations": [violation]}]
        return rep

    # 每种变异：(文件名, 说明, 报告内容, 拒绝理由里必须出现的字样)
    cases: list[tuple[str, str, object, str]] = []

    unknown = one_violation()
    unknown["severity"] = "fatal"
    cases.append(("unknown-severity.json", "严重级别 'fatal' 不认识",
                  wrap(unknown), "严重级别"))

    missing = one_violation()
    del missing["type"]
    cases.append(("missing-type.json", "违规缺 type 字段",
                  wrap(missing), "缺字段"))

    bad_items = one_violation()
    bad_items["items"] = {"not": "a list"}
    cases.append(("items-not-a-list.json", "items 不是列表",
                  wrap(bad_items), "items 不是列表"))

    cases.append(("truncated.json", "报告被截断（半截 JSON）", None, "不是合法 JSON"))
    cases.append(("toplevel-array.json", "顶层是数组不是对象", [1, 2, 3], "顶层"))

    badsheets = json.loads(json.dumps(clean))
    badsheets.pop("sheets")
    cases.append(("no-sheets.json", "缺顶层 sheets", badsheets, "缺顶层字段"))

    noviolations = json.loads(json.dumps(clean))
    noviolations["sheets"] = [{"path": "Root", "uuid_path": "/"}]
    cases.append(("sheet-without-violations.json",
                  "sheet 里缺 violations（缺列表≠没有违规）", noviolations,
                  "缺 violations"))

    for name, why, payload, expect in cases:
        path = SCRATCH / name
        if payload is None:
            path.write_text(
                (FIXTURES / "real-warning-and-error.json")
                .read_text(encoding="utf-8")[: 400], encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                            + "\n", encoding="utf-8")
        try:
            report = erc_mod.read_report(path)
        except erc_mod.ErcReportError as exc:
            message = str(exc)
            # 拒绝理由必须是**预期的那个**缺陷：否则就是"碰巧没通过",
            # 换个无关的报错也能让这条变绿。
            check(f"{why} → 拒绝，理由对得上", expect in message,
                  f"{name}｜{message[:110]}")
            continue
        check(f"{why} → 拒绝", False,
              f"却被当成 {len(report.violations)} 条违规的合法报告读了进来")

    # 另一条通路：结构**合法**的 error 违规，items 为空。
    # 这种不该被拒（items 允许为空），而必须照样挡住——按 item 展开的实现
    # 会让它整条消失，那才是真正的假通过。
    empty = json.loads(json.dumps(clean))
    v = one_violation()
    v["items"] = []
    v["severity"] = "error"
    v["type"] = "probe_empty_items"
    empty["sheets"] = [{"path": "Root", "uuid_path": "/", "violations": [v]}]
    path = SCRATCH / "empty-items-error-accepted.json"
    path.write_text(json.dumps(empty, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    report = erc_mod.read_report(path)
    diags = erc_mod.diagnostics(report)
    check("items 为空的 error：结构合法要接受，但**仍然算错误**",
          codes(diags) == ["E-ERC-004"],
          f"诊断 {codes(diags)}（若为空说明这条违规整条消失了）")

    # 反过来：报告里出现"没见过的检查项被禁用"不该被拒
    weird = json.loads(json.dumps(clean))
    weird["ignored_checks"] = list(weird.get("ignored_checks", [])) + [
        {"key": "some_future_check", "description": "未来版本新增"}]
    path = SCRATCH / "future-ignored-check.json"
    path.write_text(json.dumps(weird, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    report = erc_mod.read_report(path)
    check("清单之外的禁用项不误拒，但记进报告",
          report.ignored_keys[-1] == "some_future_check")
    check("清单之外的禁用项不触发 W-ERC-003（只记录不告警）",
          "W-ERC-003" not in codes(erc_mod.check_report(
              report, ROOT / "output" / "buck_12v_to_3v3.kicad_sch")))


# ---- [4] 生产入口 -----------------------------------------------------------

def _write_stub(name: str, body: str) -> Path:
    """造一个 kicad-cli 替身：.cmd 转发到同名 .py。"""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    py = SCRATCH / f"{name}.py"
    py.write_text(body, encoding="utf-8")
    cmd = SCRATCH / f"{name}.cmd"
    cmd.write_text(f'@python "%~dp0{name}.py" %*\r\n', encoding="utf-8")
    return cmd


STUB_ALWAYS_FAIL = '''\
"""替身：任何调用都以退出码 1 失败（模拟 kicad-cli 起不来/崩了）。

只用 ASCII：替身由控制台的 GBK 编码写 stderr，而 `cli.run` 按 UTF-8 解码，
中文会变成乱码，看起来像产品的毛病。真 kicad-cli 写的是 UTF-8。
"""
import sys
sys.stderr.write("stub: simulated kicad-cli failure\\n")
sys.exit(1)
'''

STUB_GARBAGE_REPORT = '''\
"""替身：退出码 0，但写出的报告是半截 JSON（模拟报告写坏了）。"""
import sys
out = None
argv = sys.argv[1:]
if "--version" in argv:
    print("10.0.6")
    sys.exit(0)
for i, a in enumerate(argv):
    if a == "--output":
        out = argv[i + 1]
if out is None:
    sys.exit(0)
open(out, "w", encoding="utf-8").write('{"sheets": [{"path": "Root", ')
sys.exit(0)
'''


def section_entrypoint() -> None:
    print("\n[4] 生产入口退出码（跑真的 gen.main）")
    import gen

    build = SCRATCH / "entry"
    build.mkdir(parents=True)

    def run_cli(exe: Path | None) -> int:
        old = os.environ.get(CLI_ENV)
        try:
            if exe is None:
                os.environ.pop(CLI_ENV, None)
            else:
                os.environ[CLI_ENV] = str(exe)
            return gen.main([str(EXAMPLE), "-o", str(build), "--profile", "schematic"])
        finally:
            if old is None:
                os.environ.pop(CLI_ENV, None)
            else:
                os.environ[CLI_ENV] = old

    real_cli = find_cli()
    if real_cli is None:
        print("  （本机没有 kicad-cli，跳过真实基线）")
    else:
        code = run_cli(real_cli)
        check("真 kicad-cli：0 错 0 警告 → 退出码 0", code == 0, f"退出码 {code}")

    code = run_cli(_write_stub("stub_fail", STUB_ALWAYS_FAIL))
    check("工具失败（退出码 1）→ 退出码 3，不当成零错误", code == 3,
          f"退出码 {code}")

    code = run_cli(_write_stub("stub_garbage", STUB_GARBAGE_REPORT))
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in build.glob("*/build-report.json")]
    check("半截报告真正到达 ERC 解析器（不能被前置工具/封装门禁冒充）",
          any("E-ERC-001" in {d["code"] for d in r["diagnostics"]} for r in reports))
    check("工具退出 0 但报告半截 → 退出码 3，不当成零错误", code == 3,
          f"退出码 {code}")


# ---- [5] 被禁用的重要检查 ---------------------------------------------------

def section_ignored_checks() -> None:
    print("\n[5] 被禁用的重要检查必须露出来")
    rep_path = FIXTURES / "real-ignored-important.json"
    report = erc_mod.read_report(rep_path)
    diags = erc_mod.check_report(report, ROOT / "output" / "buck_12v_to_3v3.kicad_sch")
    w3 = [d for d in diags if d.code == "W-ERC-003"]
    check("pin_not_connected 被禁用 → 1 条 W-ERC-003", len(w3) == 1,
          str(codes(diags)))
    if w3:
        print(f"        {w3[0].message}")
    check("W-ERC-003 指到具体检查项", bool(w3) and w3[0].rule_id == "pin_not_connected")
    clean = erc_mod.read_report(FIXTURES / "real-clean.json")
    check("没禁用重要检查时不出 W-ERC-003",
          "W-ERC-003" not in codes(erc_mod.check_report(
              clean, ROOT / "output" / "buck_12v_to_3v3.kicad_sch")))


def main() -> int:
    print("T06 验收探针 —— ERC 适配器")
    print(f"仓库根目录：{ROOT}")
    print(f"kicad-cli：{find_cli() or '未找到'}")

    # 真实报告基线：先跑一次真 ERC，把报告留在本目录（证据）
    if find_cli() is not None:
        tmp = tempfile.TemporaryDirectory()
        sch = write_schematic(load(EXAMPLE), Path(tmp.name))
        write_project(sch)
        report, diags = erc_mod.verify(sch, HERE / "gen-real.erc.json")
        print(f"\n真实 ERC（本目录 gen-real.erc.json）："
              f"{'不可用' if report is None else f'{len(report.violations)} 条违规'}"
              f"，诊断 {codes(diags)}")
        check("真实工程：ERC 0 违规且无诊断", report is not None and
              not report.violations and not diags, str(codes(diags)))
        tmp.cleanup()
    else:
        print("\n（本机没有 kicad-cli：只跑 fixture 部分，真实性那段标记为未运行）")

    section_real()
    section_malformed()
    section_entrypoint()
    section_ignored_checks()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"探针结论：{len(FAILURES)} 项不通过")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("探针结论：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
