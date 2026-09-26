"""ERC 报告适配器：把 kicad-cli 的 ERC 输出变成**逐违规**诊断（T06）。

为什么不能只看"错误数"：

- **一次违规可以带多个 items**（同一规则命中多个对象）。按 item 拆开会虚增
  数量；把 items 丢掉会**少报**——"items 为空"的违规在按 item 展开的实现里
  会整条消失，这正是要拦的假通过。
- **报告是文件**：可能是上一次跑剩下的。源文件、版本、时间三重对齐才认，
  跑之前先删旧报告，杜绝"拿旧结果当本次结论"。
- **`ignored_checks` 意味着有些检查根本没跑**。"0 错误 0 警告"必须让人看见
  这句话的边界：哪些检查没运行、其中有没有我们结论所依赖的那几个。
- **严重级别/字段不认识就报错**，不猜。未知 severity、缺 `type`/`severity`/
  `items`、顶层结构不对、JSON 损坏，一律 `E-ERC-001` 报告错误——宁可失败，
  也不返回"0 错误"的假通过（血的教训：官方符号没解析出来时 ERC 全是违规，
  旧解析器却报告 0 错误 0 警告）。

豁免是**登记制**：只有 warning 可以豁免，条目必须写清具体规则、对象、理由和
审查记录；没登记过的告警一律 `W-ERC-002` 露出来，不会自动进白名单。error
永远不被豁免——清单里写了也不行（会另报 `E-ERC-005`）。

本模块是 ERC 的唯一入口（`cli.erc()` 已删除，不再有第二个解析器）。
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from diagnostics import Diagnostic

from backend.kicad import cli

#: 只认这两种严重级别。别的（含未来版本新增的）一律报报告结构错误：
#: 猜一个"大概是警告吧"会让 error 悄悄降级。
KNOWN_SEVERITIES = ("error", "warning")

#: 违规必须具备的字段。缺一个 → 报告结构错误。
VIOLATION_FIELDS = ("type", "severity", "description", "items")

#: 报告顶层必须具备的字段（`sheets` 里的每个 sheet 还必须有 `violations`）。
REPORT_FIELDS = ("sheets", "kicad_version", "included_severities")

#: 豁免清单默认位置。
ALLOWLIST = Path(__file__).with_name("erc_allowlist.json")

#: 若这些检查被项目设置禁用（出现在 `ignored_checks` 里），要告警：
#: 本次"引脚/符号连接正确"的结论直接建立在它们之上，没跑就是没验证。
#: 这三个 id 来自本工程实测到的 violation type（见 output/t06-evidence/），
#: 不是从文档里抄的清单；清单之外被禁用的检查只在报告里记录，不告警。
IMPORTANT_CHECKS = frozenset({
    "lib_symbol_issues",       # 符号/引脚解析失败——旧实现假通过的根源
    "pin_not_connected",       # 引脚悬空
    "power_pin_not_driven",    # 电源脚没有驱动源
})

#: 报告时间与原理图 mtime 的比较容差（秒）。两者由同一台机器连着写出来，
#: 秒级抖动不该判成"陈旧"。
FRESHNESS_TOLERANCE = 2.0


class ErcReportError(ValueError):
    """报告本身不可用（不存在/空/损坏/结构不符/级别未知）。"""


# ---- 报告模型 ---------------------------------------------------------------

@dataclass(frozen=True)
class ErcItem:
    """违规命中的一个对象。`uuid` 是原理图里的对象 id，最可靠。"""

    description: str = ""
    uuid: str = ""
    x: float | None = None
    y: float | None = None

    @property
    def where(self) -> str:
        if self.uuid:
            return self.uuid
        if self.x is not None and self.y is not None:
            return f"({self.x}, {self.y})"
        return "?"


@dataclass(frozen=True)
class ErcViolation:
    """一条违规。`items` 原样保留（可为空，但**不因此丢掉这条违规**）。"""

    type: str
    severity: str
    description: str
    items: tuple[ErcItem, ...] = ()
    sheet: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    @property
    def uuids(self) -> tuple[str, ...]:
        return tuple(i.uuid for i in self.items if i.uuid)

    @property
    def object_id(self) -> str:
        """稳定定位串：`<规则>#<对象 id 列表>`（没有对象时是 `-`）。"""
        return f"{self.type}#{','.join(self.uuids) or '-'}"

    def render(self) -> str:
        where = "; ".join(i.description or i.where for i in self.items)
        return f"{self.description}" + (f"（{where}）" if where else "（无对象信息）")


@dataclass(frozen=True)
class ErcReport:
    path: Path
    source: str = ""
    date: str = ""
    kicad_version: str = ""
    included_severities: tuple[str, ...] = ()
    ignored_checks: tuple[tuple[str, str], ...] = ()     # (key, description)
    violations: tuple[ErcViolation, ...] = ()
    sheets: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[ErcViolation, ...]:
        return tuple(v for v in self.violations if v.is_error)

    @property
    def warnings(self) -> tuple[ErcViolation, ...]:
        return tuple(v for v in self.violations if not v.is_error)

    @property
    def ignored_keys(self) -> tuple[str, ...]:
        return tuple(k for k, _ in self.ignored_checks)


# ---- 解析：结构不符一律失败 -------------------------------------------------

def parse_report(text: str, *, path: Path | str = "<text>") -> ErcReport:
    """解析 ERC JSON 报告。**没有内容不等于零违规**，所以空/损坏都抛异常。"""
    if not text.strip():
        raise ErcReportError(f"{path}: ERC 报告为空")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ErcReportError(
            f"{path}: ERC 报告不是合法 JSON（{exc}）——可能被截断或写坏了"
        ) from exc
    if not isinstance(data, dict):
        raise ErcReportError(
            f"{path}: ERC 报告顶层是 {type(data).__name__}，期望对象")
    missing = [k for k in REPORT_FIELDS if k not in data]
    if missing:
        raise ErcReportError(
            f"{path}: ERC 报告缺顶层字段 {missing}（实际键："
            f"{sorted(data.keys())}）——结构未知，拒绝当作零违规")

    sheets = data["sheets"]
    if not isinstance(sheets, list) or not sheets:
        raise ErcReportError(f"{path}: ERC 报告的 sheets 不是非空列表")

    violations: list[ErcViolation] = []
    sheet_paths: list[str] = []
    for idx, sheet in enumerate(sheets):
        if not isinstance(sheet, dict):
            raise ErcReportError(f"{path}: sheets[{idx}] 不是对象")
        sheet_paths.append(str(sheet.get("path", f"[{idx}]")))
        if "violations" not in sheet:
            raise ErcReportError(
                f"{path}: sheets[{idx}] 缺 violations 字段——"
                f"缺少列表不等于没有违规")
        for pos, raw in enumerate(sheet["violations"]):
            violations.append(_violation(raw, where=f"sheets[{idx}].violations[{pos}]",
                                         sheet=sheet_paths[-1], path=path))

    severities = data["included_severities"]
    if not isinstance(severities, list) or not all(
            isinstance(s, str) for s in severities):
        raise ErcReportError(
            f"{path}: included_severities 不是字符串列表：{severities!r}")

    ignored = data.get("ignored_checks", [])
    if not isinstance(ignored, list):
        raise ErcReportError(f"{path}: ignored_checks 不是列表：{ignored!r}")
    ignored_pairs: list[tuple[str, str]] = []
    for idx, item in enumerate(ignored):
        if not isinstance(item, dict) or "key" not in item:
            raise ErcReportError(
                f"{path}: ignored_checks[{idx}] 不是带 key 的对象：{item!r}")
        ignored_pairs.append((str(item["key"]), str(item.get("description", ""))))

    return ErcReport(
        path=Path(path), source=str(data.get("source", "")),
        date=str(data.get("date", "")),
        kicad_version=str(data["kicad_version"]),
        included_severities=tuple(severities),
        ignored_checks=tuple(ignored_pairs),
        violations=tuple(violations), sheets=tuple(sheet_paths))


def _violation(raw, *, where: str, sheet: str, path) -> ErcViolation:
    if not isinstance(raw, dict):
        raise ErcReportError(f"{path}: {where} 不是对象：{raw!r}")
    missing = [k for k in VIOLATION_FIELDS if k not in raw]
    if missing:
        raise ErcReportError(
            f"{path}: {where} 缺字段 {missing}（实际键 {sorted(raw.keys())}）"
            f"——缺字段的违规不能当作没发生")
    severity = raw["severity"]
    if severity not in KNOWN_SEVERITIES:
        raise ErcReportError(
            f"{path}: {where} 的严重级别 '{severity}' 未知（已知："
            f"{list(KNOWN_SEVERITIES)}）——拒绝猜测，请升级本解析器")
    items = raw["items"]
    if not isinstance(items, list):
        raise ErcReportError(f"{path}: {where} 的 items 不是列表：{items!r}")
    parsed_items = []
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            raise ErcReportError(f"{path}: {where}.items[{pos}] 不是对象")
        pos_xy = item.get("pos") or {}
        x = pos_xy.get("x") if isinstance(pos_xy, dict) else None
        y = pos_xy.get("y") if isinstance(pos_xy, dict) else None
        parsed_items.append(ErcItem(
            description=str(item.get("description", "")),
            uuid=str(item.get("uuid", "")),
            x=x if isinstance(x, (int, float)) else None,
            y=y if isinstance(y, (int, float)) else None))
    return ErcViolation(type=str(raw["type"]), severity=str(severity),
                        description=str(raw["description"]),
                        items=tuple(parsed_items), sheet=sheet)


def read_report(path: Path) -> ErcReport:
    """读文件并解析。任何问题都抛 `ErcReportError`（调用方转成诊断）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ErcReportError(f"{path}: ERC 报告读不到：{exc}") from exc
    return parse_report(text, path=path)


# ---- 跑工具：失败返回诊断，不抛异常 -----------------------------------------

def cli_version() -> str:
    """`kicad-cli --version`。失败返回空串（不猜版本）。"""
    try:
        proc = cli.run(["--version"])
    except (cli.KicadError, subprocess.SubprocessError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def run(sch: Path, report_path: Path, *,
        timeout: int = cli.TIMEOUT) -> tuple[ErcReport | None, list[Diagnostic]]:
    """跑一次 ERC 并把报告读回来。

    **先删旧报告**：报告是同名文件，上一次的残留会让"本次结论"变成旧数据。
    工具缺失、超时、非零退出、报告没生成、结构不符，全部转成诊断返回
    （调用方按阶段处理），不在这里抛异常。
    """
    if cli.find_cli() is None:
        return None, [_problem("E-ERC-002",
            f"未找到 kicad-cli，ERC 未运行——"
            f"「没有错误」这句话本次没有依据。安装 KiCad 或设 {cli.CLI_ENV}。",
            sch)]

    report_path = Path(report_path)
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return None, [_problem("E-ERC-002",
                               f"无法清理旧 ERC 报告 {report_path}：{exc}", sch)]

    try:
        proc = cli.run(["sch", "erc", "--format", "json", "--output",
                        str(report_path), str(sch)], timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, [_problem("E-ERC-002",
            f"ERC 超时（>{timeout}s）：{sch.name}——超时不等于没有违规", sch)]
    except (cli.KicadError, OSError) as exc:
        return None, [_problem("E-ERC-002", f"ERC 执行失败：{exc}", sch)]

    if proc.returncode != 0:
        summary = (proc.stdout + "\n" + proc.stderr).strip().replace("\n", " | ")
        return None, [_problem("E-ERC-002",
            f"ERC 非零退出（{proc.returncode}）：{summary[:400]}", sch,
            evidence=str(report_path))]
    if not report_path.exists():
        return None, [_problem("E-ERC-002",
            f"ERC 返回 0 但没有生成报告 {report_path}——拒绝把「没有报告」"
            f"当作「没有违规」", sch)]

    try:
        report = read_report(report_path)
    except ErcReportError as exc:
        # 报告坏了是**报告**的问题（E-ERC-001），不是工具没跑起来
        # （E-ERC-002）——两者的处置不同，不能混成一个码。
        return None, [_problem("E-ERC-001", str(exc), sch,
                               evidence=str(report_path))]
    return report, []


def _problem(code: str, message: str, sch: Path, *, evidence: str = ""
             ) -> Diagnostic:
    return Diagnostic(code, "error", str(sch), message, stage="erc",
                      object_id=sch.name, evidence=evidence)


# ---- 报告是否对应"本次这个输入" ---------------------------------------------

def check_report(report: ErcReport, sch: Path, *,
                 cli_version_string: str = "") -> list[Diagnostic]:
    """报告与本次输入的对应关系。不对应 → `E-ERC-003`（陈旧/张冠李戴）。"""
    diags: list[Diagnostic] = []

    def stale(message: str, **extra) -> None:
        diags.append(Diagnostic(
            "E-ERC-003", "error", str(report.path), message, stage="erc",
            object_id=sch.name, **extra))

    if report.source and Path(report.source).name != sch.name:
        stale(f"ERC 报告说的是 '{report.source}'，本次校验的是 '{sch.name}'"
              f"——报告与输入不对应", actual=report.source, expected=sch.name)

    if "error" not in report.included_severities:
        stale(f"ERC 报告未包含 error 级别（included_severities="
              f"{list(report.included_severities)}）——这份「0 错误」没有意义")

    if report.kicad_version and cli_version_string and \
            report.kicad_version != cli_version_string:
        stale(f"ERC 报告来自 KiCad {report.kicad_version}，当前 CLI 是 "
              f"{cli_version_string}——报告不是本次工具产出的",
              actual=report.kicad_version, expected=cli_version_string)

    try:
        sch_mtime = sch.stat().st_mtime
    except OSError:
        return diags
    try:
        report_mtime = report.path.stat().st_mtime
    except OSError:
        report_mtime = None
    if report_mtime is not None and report_mtime < sch_mtime - FRESHNESS_TOLERANCE:
        stale(f"ERC 报告（{_ts(report_mtime)}）早于原理图（{_ts(sch_mtime)}）"
              f"——是上一次跑剩下的")
    parsed_date = _parse_date(report.date)
    if parsed_date is not None and parsed_date < sch_mtime - FRESHNESS_TOLERANCE:
        stale(f"ERC 报告时间 {report.date} 早于原理图最后修改时间 "
              f"{_ts(sch_mtime)}——报告描述的不是这一版原理图")

    for key, desc in report.ignored_checks:
        if key in IMPORTANT_CHECKS:
            diags.append(Diagnostic(
                "W-ERC-003", "warning", str(report.path),
                f"ERC 检查项 '{key}'（{desc}）在项目设置里被禁用，本次**没有"
                f"运行**——本次结论不覆盖它", stage="erc",
                object_id=key, rule_id=key))
    return diags


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S")


def _parse_date(raw: str) -> float | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


# ---- 豁免清单（登记制） -----------------------------------------------------

@dataclass(frozen=True)
class Exemption:
    """一条已登记的豁免。**只有 warning 可以豁免**，且必须写清理由与审查记录。"""

    type: str
    object_id: str          # 具体对象 uuid，或 "*" 表示该规则的全部对象
    reason: str
    reviewed: str           # 审查记录出处（文件/章节/评审 id）
    severity: str = "warning"
    date: str = ""

    def matches(self, violation: ErcViolation) -> bool:
        if self.type != violation.type:
            return False
        if self.object_id == "*":
            return True
        return self.object_id in violation.uuids


def load_exemptions(path: Path | str = ALLOWLIST
                    ) -> tuple[tuple[Exemption, ...], list[Diagnostic]]:
    """读豁免清单。条目非法 → `E-ERC-005`，且**该条目不生效**。"""
    path = Path(path)
    if not path.exists():
        return (), []
    diags: list[Diagnostic] = []

    def bad(message: str, **extra) -> None:
        diags.append(Diagnostic("E-ERC-005", "error", str(path), message,
                                stage="erc", **extra))

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        bad(f"豁免清单读不出来：{exc}")
        return (), diags
    if not isinstance(data, dict) or not isinstance(data.get("exemptions"), list):
        bad(f"豁免清单顶层必须是 {{\"exemptions\": [...]}}，实际是 "
            f"{type(data).__name__}")
        return (), diags

    out: list[Exemption] = []
    for idx, raw in enumerate(data["exemptions"]):
        where = f"exemptions[{idx}]"
        if not isinstance(raw, dict):
            bad(f"{where} 不是对象", object_id=where)
            continue
        missing = [k for k in ("type", "object_id", "reason", "reviewed")
                   if not raw.get(k)]
        if missing:
            bad(f"{where} 缺字段 {missing}——豁免必须有规则、对象、理由和审查记录",
                object_id=where)
            continue
        if raw.get("severity", "warning") != "warning":
            bad(f"{where} 试图豁免 '{raw.get('severity')}' 级违规——"
                f"只有 warning 可以豁免，error 必须修", object_id=where)
            continue
        out.append(Exemption(
            type=str(raw["type"]), object_id=str(raw["object_id"]),
            reason=str(raw["reason"]), reviewed=str(raw["reviewed"]),
            severity="warning", date=str(raw.get("date", ""))))
    return tuple(out), diags


# ---- 违规 → 诊断 -------------------------------------------------------------

def diagnostics(report: ErcReport,
                exemptions: Sequence[Exemption] = ()) -> list[Diagnostic]:
    """逐违规出诊断。

    - error：一律 `E-ERC-004`（**不可豁免**；清单里写了也只会多一条提示）。
    - warning：命中登记条目 → `W-ERC-001`（带理由与审查记录）；
      否则 → `W-ERC-002`（露出来，不进白名单）。
    """
    diags: list[Diagnostic] = []
    for v in report.violations:
        hit = next((e for e in exemptions if e.matches(v)), None)
        common = dict(stage="erc", object_id=v.object_id, rule_id=v.type,
                      evidence=v.render())
        if v.is_error:
            note = ""
            if hit is not None:
                note = (f"（豁免清单里登记了 '{v.type}'，但豁免只适用于 "
                        f"warning：{hit.reason}）")
            diags.append(Diagnostic(
                "E-ERC-004", "error", str(report.path),
                f"未豁免的 ERC 错误 [{v.type}] {v.render()}{note}", **common))
        elif hit is not None:
            diags.append(Diagnostic(
                "W-ERC-001", "warning", str(report.path),
                f"已登记豁免的 ERC 告警 [{v.type}] {v.render()}"
                f"——理由：{hit.reason}（审查记录：{hit.reviewed}）", **common))
        else:
            diags.append(Diagnostic(
                "W-ERC-002", "warning", str(report.path),
                f"未登记的 ERC 告警 [{v.type}] {v.render()}——"
                f"确属预期请在 {ALLOWLIST.name} 里登记规则、对象、理由与审查记录",
                **common))
    return diags


# ---- 一次做完：跑 + 对应性 + 豁免 + 诊断 -------------------------------------

def verify(sch: Path, report_path: Path, *,
           exemptions: Sequence[Exemption] | None = None,
           allowlist: Path | str = ALLOWLIST,
           timeout: int = cli.TIMEOUT
           ) -> tuple[ErcReport | None, list[Diagnostic]]:
    """生产入口用这一个函数：跑 ERC、核对应关系、按豁免出诊断。

    `exemptions=None` 时从 `allowlist` 读（读不动会带上 `E-ERC-005`）。
    """
    diags: list[Diagnostic] = []
    if exemptions is None:
        exemptions, load_diags = load_exemptions(allowlist)
        diags += load_diags
    report, run_diags = run(sch, report_path, timeout=timeout)
    diags += run_diags
    if report is None:
        return None, diags
    diags += check_report(report, sch, cli_version_string=cli_version())
    diags += diagnostics(report, exemptions)
    return report, diags
