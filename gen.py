"""CircuitOS 前端：IR → 工程文件 → 校验 → 报告。

用法:
    python gen.py examples/buck_12v_to_3v3.json [-o output]

流程：加载 IR → validate（有 error 直接停，不产垃圾文件）→ 生成
.kicad_sch + .kicad_pro → 找到 kicad-cli 就自动跑 ERC + 导出网表，
找不到就跳过并提示（工程文件仍已生成）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.kicad.cli import erc, export_netlist, find_cli
from backend.kicad.project import write_project
from backend.kicad.schematic import write_schematic
from ir.schema import load
from ir.validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CircuitOS：IR → KiCad 工程")
    parser.add_argument("ir_file", help="IR JSON 文件")
    parser.add_argument("-o", "--out", default="output", help="输出目录")
    args = parser.parse_args(argv)

    ir = load(Path(args.ir_file))
    outcome = validate(ir)
    for d in outcome.diagnostics:
        print(f"[{d.code}] {d.severity} {d.path} — {d.message}")
    if not outcome.ok:
        print(f"\nIR 校验失败：{len(outcome.errors)} 个错误，未生成工程文件。")
        return 1

    out_dir = Path(args.out)
    sch = write_schematic(ir, out_dir)
    pro = write_project(sch)
    print(f"已生成: {sch.name} / {pro.name}（官方符号已内嵌，自包含）")

    if find_cli() is None:
        print("未找到 kicad-cli，跳过 ERC/网表核验。"
              "安装 KiCad 或设 COS_KICAD_CLI 指向 kicad-cli 后可自动核验。")
        return 0

    errors, warnings = erc(sch)
    print(f"\nERC: {len(errors)} 个错误, {len(warnings)} 个警告")
    for e in errors:
        print(f"  E: {e}")
    for w in warnings:
        print(f"  W: {w}")
    net = export_netlist(sch, out_dir / f"{ir.project}.net")
    print(f"网表已导出: {net.name}")
    if errors:
        print("\nERC 有错误——工程文件已生成但未通过电气检查。")
        return 2
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
