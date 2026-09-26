"""器件必需外围子图的**规则定义**（纯数据，不 import 任何东西）。

数据手册里"这个脚必须这样接"的要求，光靠引脚表表达不了：引脚表只说
「BST 是 passive」，说不出「BST 与 SW 之间必须有一只电容」。此前这类要求
只写在文档和提示词里，于是 M1 的自举电容接在 BST–GND 上照样通过所有检查
（审查报告 H3）。

规则实例作为数据挂在器件上（`Part.connection_rules`，见 parts/partsdb.py），
检查器只有一份（checks/subgraph.py）。定义与检查分开放，是为了避免
「partsdb → rules → 检查器 → partsdb」的循环导入。

每条规则都带页码依据，报错时能直接说清「依据哪一页判它错」。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapBetween:
    """两个脚之间必须有一只电容（自举电容）。脚名按器件资料的叫法写。"""
    pin_a: str
    pin_b: str
    evidence: str


@dataclass(frozen=True)
class RToNet:
    """某个脚必须经电阻接到指定网络（如 FREQ 对地电阻设定频率）。"""
    pin: str
    net: str
    evidence: str


@dataclass(frozen=True)
class SeriesRCToNet:
    """某个脚必须经「电阻串联电容」接到指定网络（如 COMP 补偿支路）。"""
    pin: str
    net: str
    evidence: str


@dataclass(frozen=True)
class NotTiedToPin:
    """某个脚不得与同一器件的另一个脚同网（如 EN 不得直接接 VIN）。"""
    pin: str
    other_pin: str
    why: str
    evidence: str
