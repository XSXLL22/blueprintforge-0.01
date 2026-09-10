# vendor 符号库

本目录的 `.kicad_sym` 是从 **KiCad 10.0.6 官方符号库**（安装目录
`share/kicad/symbols/`）抽取的原始符号定义，**未经修改**（仅保留所需符号，
文件头版本戳 `20251024` 原样保留）。

## 来源与许可

- 来源：KiCad 10.0.6 官方符号库（KiCad 项目）
- 许可：**CC-BY-SA-4.0**（与 KiCad 官方库一致）
- 抽取工具：手动保留所需 `(symbol ...)` 块；目录内不包含官方库的其余符号

## 为什么 vendor 进来

kicad-cli 跑 ERC/导网表时，符号定义**只认 `.kicad_sch` 里 `(lib_symbols)`
内嵌的副本，完全不查 sym-lib-table**（实测：表里写了对的库、绝对路径、
环境变量 URI 全都没用）。把官方符号 vendor 进仓库、渲染时整体内嵌，
输出文件才自包含——不装 KiCad 的机器也能生成同样的工程文件。

## 文件清单

| 文件 | 内容 | 用途 |
|------|------|------|
| `Device.kicad_sym` | R / C / L / D_Schottky | 无源器件、二极管 |
| `power.kicad_sym` | GND / PWR_FLAG | 电源符号 |
| `Connector_Generic.kicad_sym` | Conn_01x02 | 2P 接插件 |

新增符号：从 KiCad 官方库抽 `(symbol "NAME" ...)` 整块（括号配平），在
`backend/kicad/symlib.py` 的 `OFFICIAL` 表登记 `lib_id → (文件名, 符号名)`。
