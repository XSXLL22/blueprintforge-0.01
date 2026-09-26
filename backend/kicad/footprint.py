"""读取 KiCad 官方封装（.kicad_mod）的焊盘表。

用途：**器件目录自校验**——符号脚号必须与所选封装的电气焊盘号一致，
否则 PCB 上那个焊盘会悬空。散热焊盘尤其容易想当然：SOIC-8-1EP 系列的
散热焊盘是不是 `9`、有没有 `pad_prop_heatsink`、有没有额外的无编号
钢网焊盘，只能读实际封装文件，不能凭经验写。

只依赖本目录的 sexpr 解析器，不做几何计算。

环境变量 `COS_KICAD_FOOTPRINTS` 可指定封装库根目录（CI 用）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from backend.kicad import sexpr
from backend.kicad.cli import find_cli

#: 覆盖封装库根目录的环境变量名。
FOOTPRINTS_ENV = "COS_KICAD_FOOTPRINTS"

#: 铜层判定：F.Cu / B.Cu / In*.Cu / *.Cu。
def _is_copper(layer: str) -> bool:
    return layer.endswith(".Cu")


@dataclass(frozen=True)
class Pad:
    """一个焊盘。`number` 为空 = 无编号（非电气，例如只开钢网的散热开窗）。"""
    number: str
    ptype: str                       # smd | thru_hole | np_thru_hole | connect
    shape: str                       # rect | circle | oval | roundrect | custom
    at: tuple[float, float]
    size: tuple[float, float]
    layers: tuple[str, ...]
    properties: tuple[str, ...]      # pad_prop_heatsink / _castellated / _mechanical ...
    drill: float | None = None

    @property
    def is_npth(self) -> bool:
        return self.ptype == "np_thru_hole"

    @property
    def is_electrical(self) -> bool:
        """是否是可连网的电气焊盘：有编号、不是 NPTH、且有铜层。"""
        return (self.number != "" and not self.is_npth
                and any(_is_copper(x) for x in self.layers))

    @property
    def is_heatsink(self) -> bool:
        return "pad_prop_heatsink" in self.properties

    def describe(self) -> str:
        bits = [f"pad {self.number or '(无编号)'}", self.ptype, self.shape,
                f"{self.size[0]}x{self.size[1]}mm"]
        if self.properties:
            bits.append(" ".join(self.properties))
        return " ".join(bits)


class FootprintError(RuntimeError):
    """封装库找不到或封装文件损坏。"""


def footprints_root(env: dict[str, str] | None = None) -> Path | None:
    """KiCad 官方封装库根目录（形如 `.../share/kicad/footprints`）。"""
    env = os.environ if env is None else env
    override = env.get(FOOTPRINTS_ENV)
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    cli = find_cli(env)
    if cli is None:
        return None
    # <root>/bin/kicad-cli(.exe) → <root>/share/kicad/footprints
    candidate = cli.parent.parent / "share" / "kicad" / "footprints"
    return candidate if candidate.is_dir() else None


def footprint_path(lib_id: str, env: dict[str, str] | None = None) -> Path | None:
    """`库:名` → `.kicad_mod` 路径；找不到返回 None。"""
    if ":" not in lib_id:
        return None
    lib, name = lib_id.split(":", 1)
    root = footprints_root(env)
    if root is None:
        return None
    path = root / f"{lib}.pretty" / f"{name}.kicad_mod"
    return path if path.is_file() else None


def _pads_from_text(text: str, where: str) -> tuple[Pad, ...]:
    try:
        root = sexpr.parse_one(text)
    except sexpr.SExprError as exc:
        raise FootprintError(f"{where} 解析失败：{exc}") from exc

    pads: list[Pad] = []
    for node in sexpr.children(root, "pad"):
        if len(node) < 2:
            raise FootprintError(f"{where} 出现不完整的 pad 节点")
        number = str(node[1])
        ptype = str(node[2]) if len(node) > 2 else ""
        shape = str(node[3]) if len(node) > 3 else ""
        at = sexpr.values_of(node, "at")
        size = sexpr.values_of(node, "size")
        drill = sexpr.values_of(node, "drill")
        pads.append(Pad(
            number=number,
            ptype=ptype,
            shape=shape,
            at=(_f(at, 0), _f(at, 1)),
            size=(_f(size, 0), _f(size, 1)),
            layers=tuple(str(x) for x in sexpr.values_of(node, "layers")),
            properties=tuple(str(x) for x in sexpr.values_of(node, "property")),
            drill=_opt_f(drill, 0),
        ))
    return tuple(pads)


def _f(values: list, index: int) -> float:
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return 0.0


def _opt_f(values: list, index: int) -> float | None:
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return None


@lru_cache(maxsize=512)
def _cached(lib_id: str, path_str: str, mtime: float) -> tuple[Pad, ...]:
    return _pads_from_text(Path(path_str).read_text(encoding="utf-8"), lib_id)


def pads_of(lib_id: str, env: dict[str, str] | None = None) -> tuple[Pad, ...]:
    """读某个封装的焊盘表。封装不存在时抛 FootprintError。"""
    path = footprint_path(lib_id, env)
    if path is None:
        root = footprints_root(env)
        hint = (f"封装库根目录：{root}" if root
                else f"未找到 KiCad 封装库（可用 {FOOTPRINTS_ENV} 指定）")
        raise FootprintError(f"找不到封装 {lib_id!r}。{hint}")
    return _cached(lib_id, str(path), path.stat().st_mtime)


def electrical_pads(lib_id: str, env: dict[str, str] | None = None) -> tuple[Pad, ...]:
    """只返回可连网的电气焊盘。"""
    return tuple(p for p in pads_of(lib_id, env) if p.is_electrical)
