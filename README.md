# CircuitOS（工作名）—— 电路 AI 操作系统

用户提出自然语言需求 → 大模型产出结构化 **IR**（中间表示）→ 确定性后端把 IR
加工成 KiCad 工程文件并逐级验证 → 交付可打样的制造文件。

继承自 BlueprintForge（`E:\deepseek\2026-9-1`）的工程闭环遗产（kicad-cli 无头
自动化、`PcbResult.ok` 确定性判据、错误分类有界修复、纯标准库零依赖），替换其
「74HC 离散门」设计哲学为「模块级 + IR + 市场资材」的新路线。

## 技术路线

```
自然语言需求 + 约束
    ↓  Claude（架构师：读 skill + 器件条目，产出 IR）
IR (JSON) —— 与 EDA 无关的中间表示，LLM 与后端的唯一交接面
    ↓
├─ ir/validate.py     确定性校验（0 token）→ 编译器式错误码 → 有界修复循环
├─ backend/kicad      生成 .kicad_sch / .kicad_pcb（消费 IR，不依赖 LLM 文本）
├─ kicad-cli          DRC/ERC 主力验证（0 token）
├─ KiCad MCP          结构化诊断抽查（按需加载，见 DESIGN.md 第 6 节）
├─ ngspice            SPICE 仿真验证（M4）
└─ parts/             器件库（价格/库存/生命周期），stub → 供应链 API（M5）
```

## 两条铁律

1. **LLM 永不直接读写工程文件。** 它只碰 IR（约 2–5k token）和错误码反馈；
   板文件动辄几万 token，格式噪音极大，直接改必错。
2. **规则进代码，不进提示词。** 一切可确定性判断的事（器件不存在、网络悬空、
   约束违反）都写成校验器。提示词越短，长文本失误越不可能发生。

## 里程碑

| 阶段 | 目标 | 验证点 |
|------|------|--------|
| M0 ✅ | 工程骨架 + IR schema v0.1 + 校验器 + 器件库 stub | 示例 buck IR 校验干净 |
| M1 ✅ | IR → KiCad 原理图生成器（移植遗产） | ERC 0 错误 0 警告；网表 7 网与 IR 逐引脚一致；34 测试全绿 |
| M2 | PCB 布局布线 + kicad-cli DRC 闭环 | DRC 零违规 |
| M3 | 错误分类修复循环 + power-design skill | LLM 从错误码自修复 |
| M4 | ngspice 仿真（纹波/负载跳变/效率） | 仿真与理论一致 |
| M5 | 供应链 API + 打样实测反馈 | 板子回来，数据回灌 |

详细路线图与「每次开工顺序」见 [DESIGN.md](./DESIGN.md) 第 10 节。

## 快速开始

```bash
python gen.py examples/buck_12v_to_3v3.json -o output   # 生成 + ERC + 网表
python -m unittest discover -s tests -v                  # 全量测试
```

纯标准库，无第三方依赖（与 BlueprintForge 同一洁癖）。
生成物 `output/` 不进仓库；机器上装了 KiCad 会自动跑 ERC/网表核验，
没有则跳过（工程文件仍生成，符号全部内嵌、自包含）。
