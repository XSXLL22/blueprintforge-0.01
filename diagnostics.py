"""诊断模型与错误码登记表（底层，不依赖任何后端）。

约定（实施指南 4.5）：

- **调用者依赖 `code`，不依赖消息文本**。消息给人和 LLM 看，可以改；
  `code` 一旦发布就有稳定含义，不能拿旧码表达另一类错误。
- 新码必须先在 `CODES` 里登记（写清一段定义），否则构造 `Diagnostic`
  会直接报错——把拼错的错误码变成开发期错误，而不是线上静默漏检。
- `severity=error` 拦路；`warning` 可见不拦路。栏路的判断只看 error。
- 除 `path` 外还带 `stage / object_id / rule_id / evidence / actual /
  expected`：让「为什么判它错」可追溯，而不是只有一句断言。

错误码命名：`<级别>-<域>-<组>-<序号>`
  级别 E/W；域 IR/CAT/SCH/ERC/NET/BUILD；
  组 STRUCT/DECODE/ELECT/PARTS/TOPO/NETS/DOC/...
"""
from __future__ import annotations

from dataclasses import dataclass, field

ERROR = "error"
WARNING = "warning"

#: 已登记的错误码 → 定义。新增码必须先加到这里。
CODES: dict[str, str] = {
    # ---- 结构/解码（IR 输入层）----
    "E-IR-STRUCT-001": "schema_version 不受支持",
    "E-IR-STRUCT-002": "project 非法（标识符/Windows 保留名/路径分隔符）",
    "E-IR-STRUCT-003": "module_type 不在支持列表",
    "E-IR-STRUCT-004": "topology 不适用于该 module_type",
    "E-IR-DECODE-001": "顶层不是 JSON 对象",
    "E-IR-DECODE-002": "字段缺失",
    "E-IR-DECODE-003": "字段类型错误（含 bool 冒充数值）",
    "E-IR-DECODE-004": "数值非有限（NaN/Infinity/溢出指数）",
    "E-IR-DECODE-005": "未知字段（严格模式）",
    "E-IR-DECODE-006": "重复 JSON key",
    "E-IR-DECODE-007": "版本不支持或缺少迁移路径",
    "E-IR-DECODE-008": "容器为空或元素类型错误（空数组/非对象元素）",
    "E-IR-DECODE-009": "标识符语法非法（位号/引脚引用/端口名）",
    "E-IR-DECODE-010": "JSON 文本本身不可解析",
    "E-IR-DECODE-011": "目标/约束键不支持或单位不匹配",
    "E-IR-DECODE-012": "IR 里出现 NC/PWR_FLAG 等保留名冲突",
    "E-IR-DECODE-013": "字段取值不在允许集合（枚举/标记）",
    "W-IR-DECODE-001": "数值疑似量纲/单位错误（超出常理范围）",
    "W-IR-DECODE-002": "v0.1 → v0.2 迁移：值被换算，需人工确认",
    # ---- 电气约束 ----
    "E-IR-ELECT-001": "vin/vout 区间不满足 0 < min <= typ <= max",
    "E-IR-ELECT-002": "降压模块要求 vout.max < vin.min",
    "E-IR-ELECT-003": "iout_max <= 0",
    "E-IR-ELECT-004": "超出器件工作范围/绝对最大值",
    "E-IR-ELECT-005": "设计目标与所选器件能力矛盾（无法保证）",
    "W-IR-ELECT-001": "LDO 压差热耗偏大",
    "W-IR-ELECT-002": "接近器件边界（建议复核裕量）",
    "W-IR-ELECT-003": "目标未验证（缺模型/缺条件）",
    # ---- 器件与目录 ----
    "E-IR-PARTS-001": "位号重复",
    "E-IR-PARTS-002": "role 不在允许列表",
    "E-IR-PARTS-003": "器件不在目录中（查无此料）",
    "E-IR-PARTS-004": "器件已停产（EOL）",
    "E-IR-PARTS-005": "器件无库存",
    "E-IR-PARTS-006": "regulator 数量不为 1",
    "E-IR-PARTS-007": "拓扑必需器件缺失（如 buck 无电感）",
    "E-IR-PARTS-008": "role 与目录类别不符",
    "E-IR-PARTS-009": "引用了器件不存在的引脚",
    "E-IR-PARTS-010": "同一物理引脚重复归属多个网络",
    "E-IR-PARTS-011": "必接引脚没有接入任何网络",
    "E-IR-PARTS-012": "非法的不连接声明（器件不允许悬空/未声明）",
    "E-IR-PARTS-013": "同一引脚既被连接又被声明不连接",
    "E-IR-PARTS-014": "封装覆盖不在允许集合内",
    "E-IR-PARTS-015": "value 与所选料号的工程参数不一致",
    "E-IR-PARTS-016": "目录记录不完整（缺来源/额定值/封装映射）",
    "E-IR-PARTS-017": "引脚引用有歧义（脚名与脚号指向不同引脚）",
    "E-IR-PARTS-018": "该器件/料号不能进入所请求的阶段（未解析到可采购型号）",
    "W-IR-PARTS-001": "器件不建议新设计（NRFND）",
    "W-IR-PARTS-002": "value 写法与目录值接近但不相同",
    "W-IR-PARTS-003": "器件记录是泛化料号/未核对来源",
    # ---- 连接与拓扑 ----
    "E-IR-NETS-001": "网络名重复",
    "E-IR-NETS-002": "缺少 GND 网络",
    "E-IR-NETS-003": "网络名非法",
    "E-IR-NETS-004": "网络引用了不存在的位号",
    "E-IR-NETS-005": "电源网络没有可证明的来源",
    "E-IR-NETS-006": "外部端口声明与引脚连接冲突",
    "E-IR-NETS-007": "网络只有单个引脚（悬空的网）",
    "E-IR-NETS-008": "关键子图不满足模板要求",
    "E-IR-NETS-009": "同一引脚在同一网络里重复出现",
    "W-IR-NETS-001": "网络没有引脚",
    "W-IR-NETS-002": "器件没有接进任何网络",
    # ---- 文档完整性 ----
    "W-IR-DOC-001": "design_rationale 为空",
    "W-IR-DOC-002": "assumptions 为空",
    "W-IR-DOC-003": "risks 为空",
    "W-IR-DOC-004": "存在未验证的目标/参数（缺模型、缺条件或未实测）",
    # ---- 目录自校验 ----
    "E-CAT-001": "目录记录字段非法或缺失",
    "E-CAT-002": "符号脚号与封装电气焊盘不一致",
    "E-CAT-003": "引脚编号重复",
    "E-CAT-004": "引脚引用歧义",
    "E-CAT-005": "sourced 记录缺来源/页码",
    "E-CAT-006": "策略要求核对封装焊盘映射，但没有读取器或读取失败",
    "W-CAT-001": "封装不可读取（无法核对 pad 映射）",
    # ---- 生成与网表对账 ----
    "E-SCH-001": "原理图生成失败",
    "E-NET-001": "导出网表无法解析",
    "E-NET-002": "网表与 IR 的网络/引脚集合不一致",
    "E-NET-003": "网表节点数量与 IR 不符（多出或缺失）",
    "E-NET-004": "网表引脚归属与 IR 不同",
    "E-NET-005": "网表出现 IR 未声明的对象（器件/引脚，且不是本次生成的辅助符号）",
    "E-NET-006": "网表来自层级/多页原理图，当前策略不支持",
    # ---- ERC ----
    "E-ERC-001": "ERC 报告结构不符合预期（拒绝假通过）",
    "E-ERC-002": "ERC 工具缺失或执行失败",
    "E-ERC-003": "ERC 报告与本次输入不对应（陈旧/缺文件）",
    "E-ERC-004": "出现未获豁免的 ERC 错误",
    "E-ERC-005": "ERC 豁免清单条目非法（缺字段，或试图豁免 error 级）",
    "W-ERC-001": "出现已登记豁免的 ERC 告警",
    "W-ERC-002": "出现未登记的 ERC 告警",
    "W-ERC-003": "ERC 检查项本次未运行（项目设置里被禁用），结论不覆盖它",
    # ---- 流程与报告 ----
    "E-BUILD-001": "profile 未知或阶段缺失",
    "E-BUILD-002": "严格模式下必需阶段未通过",
    "E-BUILD-003": "构建目录/产物写入失败",
    "E-BUILD-004": "必需工具缺失、版本不支持或执行失败",
    "E-BUILD-005": "未豁免告警或关键检查未运行，严格门禁失败",
    "E-BUILD-006": "内部构建错误",
}


@dataclass(frozen=True)
class Diagnostic:
    """一条诊断。`path` 定位到输入字段（如 `nets[3].pins[1].pin`）。"""
    code: str
    severity: str
    path: str
    message: str
    stage: str = ""          # decode / resolve / connectivity / catalog / erc / netlist / build
    object_id: str = ""      # 稳定对象标识：位号、网络名、工具名
    rule_id: str = ""        # 规则或来源 id（如 "MP1584 Rev1.0 p.2"）
    evidence: str = ""       # 依据：文档页码、命令、文件
    actual: str = ""         # 实际值
    expected: str = ""       # 期望值

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise KeyError(
                f"未登记的错误码 {self.code!r}——请先在 diagnostics.CODES 里"
                f"登记定义，避免码义漂移")
        if self.severity not in (ERROR, WARNING):
            raise ValueError(f"未知严重级别 {self.severity!r}")

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def to_dict(self) -> dict:
        data = {"code": self.code, "severity": self.severity,
                "path": self.path, "message": self.message}
        for key in ("stage", "object_id", "rule_id", "evidence",
                    "actual", "expected"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data

    def render(self) -> str:
        """单行文本形式，用于 CLI 与日志。"""
        where = f" [{self.path}]" if self.path else ""
        return f"{self.severity.upper():7} {self.code}{where} {self.message}"


@dataclass
class Outcome:
    """一组诊断的结论。`ok` 只当没有任何 error 时为真。

    `unverified` 是**第三态**：既不是错误（不该拦路），也不是通过。
    「缺封装读取器，没能核对焊盘映射」这类事实既拦不住"仅生成"，也不允许
    被读成"核对过了"——它必须有出口（BuildReport 的 pending 列表），
    否则调用方只能从 `ok=True` 里读出"没问题"（T00 的静默通过就是这么来的）。
    """
    diagnostics: tuple[Diagnostic, ...] = ()
    unverified: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.is_error)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if not d.is_error)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "Outcome") -> "Outcome":
        return Outcome(self.diagnostics + other.diagnostics,
                       self.unverified + other.unverified)

    def render(self) -> str:
        return "\n".join(d.render() for d in self.diagnostics)


def max_severity(diags: tuple[Diagnostic, ...]) -> str:
    """一组诊断里最严重的级别；空集返回空串。"""
    if any(d.is_error for d in diags):
        return ERROR
    if diags:
        return WARNING
    return ""
