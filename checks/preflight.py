"""生成/校验共用的电气预检：解析连接 → 证明供电 → 跑器件子图规则 → 核器件事实。

为什么要有一个统一入口：这三步的结论构成「这张图在电气上是否可信」，
校验器要拿它报诊断，生成器要拿它决定**敢不敢画**。两边各调一遍、各自
判断"什么算拦路"，就会出现 T00 那种「校验说有错、生成照做」——生成器
只挡住连接冲突、却照画一个 EN 直连 12V 的分压网络。

器件事实（有效封装/value 是否与所选料一致/额定值够不够/拓扑是否被支持）
放在这里而不是留一张单独的表：它们是同一个判断的不同侧面——「这份 IR 描述的
电路能不能造出来」。分成两处就会出现同一个问题两个来源、两套阈值。

`raise_on_error=True` 用于生成器：有任何 error 就不产出文件，绝不用
"后写的赢"或"自己补个标志"把不认识的图糊过去。
"""
from __future__ import annotations

from collections.abc import Mapping

from diagnostics import Diagnostic
from ir.schema import PowerIR
from parts.partsdb import PARTS, Part

from checks import parts as parts_check
from checks import subgraph, supply as supply_check
from checks.connectivity import Connectivity, resolve
from checks.parts import PartFacts


def preflight(ir: PowerIR, parts: Mapping[str, Part] | None = None,
              *, raise_on_error: bool = False
              ) -> tuple[Connectivity, supply_check.Supply, PartFacts,
                         list[Diagnostic]]:
    """连接解析 + 供电证明 + 子图规则 + 器件事实，返回 (连接, 供电, 事实, 诊断)。

    结构层的错误（schema_version / role / 器件查无此料……）不在这里——
    那是 `ir/validate.py` 的其余部分；这里只管**连接语义**。
    连接本身有冲突时子图规则不跑（图不可信，先修冲突），见 subgraph.check。
    """
    parts = PARTS if parts is None else parts
    conn, diags = resolve(ir, parts)
    supply, supply_diags = supply_check.prove(ir, parts, conn)
    facts, fact_diags = parts_check.check(ir, parts, conn)
    diags = (list(diags) + list(supply_diags)
             + list(subgraph.check(ir, parts, conn)) + list(fact_diags))
    if raise_on_error:
        bad = [d for d in diags if d.is_error]
        if bad:
            raise ValueError(
                "连接/供电/子图/器件事实检查未通过，拒绝生成：\n"
                + "\n".join(
                    f"  {d.code} [{d.path}] {d.message}" for d in bad))
    return conn, supply, facts, diags
