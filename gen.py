"""CircuitOS CLI: explicit profiles for CI, one report for every build."""
import argparse
import json
from pathlib import Path
import sys
from pipeline.build import build, catalog_policy


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="CircuitOS: IR -> KiCad + BuildReport",
        epilog="Exit: 0=profile pass; 1=input/design; 2=verification (or argparse); "
               "3=tool/version/report; 4=I/O/internal")
    parser.add_argument("ir_file")
    parser.add_argument("-o", "--out", default="output", help="独立构建子目录的根目录")
    parser.add_argument("--profile", choices=("generate", "schematic", "auto"), default="auto")
    args = parser.parse_args(argv)
    report = build(Path(args.ir_file), Path(args.out), profile=args.profile)
    print(f"Profile: {report.profile} (requested: {report.requested_profile})")
    if report.requested_profile == "auto":
        print("自动模式仅供交互使用；CI/发布验收请显式指定 --profile schematic。")
    for name, stage in report.stages.items():
        print(f"  {name}: {stage.status} ({stage.reason})")
    for d in report.diagnostics:
        print(f"[{d['code']}] {d['severity']} {d['path']} — {d['message']}")
    for target in report.pending_targets:
        print(f"待验证目标: {target['key']} ({target['reason']})")
    for item in report.unverified:
        print(f"未核对: {item}")
    ignored = report.stages["erc"].details.get("ignored_checks", [])
    if ignored:
        print("未运行 ERC 检查: " + ", ".join(k for k, _ in ignored))
    print(report.scope)
    if report.passed_profile:
        print("原理图检查通过。" if report.profile == "schematic" else "仅生成 profile 通过。")
    else:
        print(f"构建未通过，退出码 {report.exit_code}；产物仅供诊断。")
    if report.report_path:
        print(f"BuildReport: {report.report_path}")
    else:
        print("无法落盘 BuildReport，以下为完整故障报告：", file=sys.stderr)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
