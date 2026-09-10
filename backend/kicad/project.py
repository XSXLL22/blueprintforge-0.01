"""写最小 .kicad_pro（移植自 BlueprintForge hdc/pcb/pipeline.py 的
write_project）。

.kicad_pro 只填 KiCad 必须看到的骨架，设计规则一项不写——留空由 KiCad
填默认值，与 kicad-cli 在没有工程文件时的行为一致，不会悄悄改变
DRC/ERC 的判据。

符号库不需要任何表：原理图生成器把官方符号定义直接内嵌进 .kicad_sch
的 (lib_symbols)（见 backend/kicad/symlib.py——kicad-cli 只认内嵌符号，
项目级 sym-lib-table 写了也不读，实测过）。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

#: 生成 .kicad_pro 里根图纸 uuid 用的名字空间。
_NS = uuid.UUID("8d1a2c3e-4f5a-6b7c-8d9e-0f1a2b3c4d5e")


def write_project(board_file: Path) -> Path:
    """给 `<name>.kicad_sch/.kicad_pcb` 写配套 `<name>.kicad_pro`，返回路径。

    若旁边有 schematic 生成器写的 circuitos.kicad_sym（自绘符号库），
    再补一份 sym-lib-table 把 circuitos 库登记进去——给 KiCad 图形界面
    用，也消除 ERC 的「circuitos 库不在配置中」警告（probe7 实测：
    有 .kicad_pro + 表 + 库文件即无该警告；kicad-cli 解析符号仍只认
    .kicad_sch 内嵌的 lib_symbols 副本）。
    """
    path = board_file.with_suffix(".kicad_pro")
    root = str(uuid.uuid5(_NS, board_file.stem))
    path.write_text(json.dumps({
        "board": {"design_settings": {}, "layer_presets": [], "viewports": []},
        "boards": [],
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": path.name, "version": 2},
        "net_settings": {},
        "pcbnew": {"page_layout_descr_file": ""},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [[root, "Root"]],
        "text_variables": {},
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if (board_file.parent / "circuitos.kicad_sym").is_file():
        (board_file.parent / "sym-lib-table").write_text(
            "(sym_lib_table\n"
            '  (version 7)\n'
            '  (lib (name "circuitos") (type "KiCad") '
            '(uri "${KIPRJMOD}/circuitos.kicad_sym") (options "") '
            '(descr "CircuitOS 生成符号（GUI 用；kicad-cli 用内嵌副本）"))\n'
            ")\n", encoding="utf-8")
    return path
