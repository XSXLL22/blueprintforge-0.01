"""定位并调用 kicad-cli（移植自 BlueprintForge hdc/pcb/kicad.py，接口不变）。

KiCad 的安装位置各平台差别很大，`pcbnew` 模块又只在 KiCad 自带 Python 里能
import。这里把两件事收成小接口：

- `find_cli()`：按「环境变量 → PATH → 常见安装目录」的顺序找。
- `run()`：统一 UTF-8 解码与超时，返回 CompletedProcess。
- `erc()` / `export_netlist()`：原理图验证的专用封装。

用环境变量 `COS_KICAD_CLI` 可以指定任意路径，便于 CI。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

#: 覆盖自动查找的环境变量名。
CLI_ENV = "COS_KICAD_CLI"

#: 命令默认超时（秒）。
TIMEOUT = 300

#: KiCad 主版本目录名（新版本在前，找到即用）。
_VERSIONS = ("10.0", "9.0", "8.0", "7.0")


class KicadError(RuntimeError):
    """KiCad 缺失或命令执行失败。"""


def _windows_roots(env: Mapping[str, str]) -> list[Path]:
    keys = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
    bases = [Path(env[k]) / "Programs" / "KiCad" for k in keys if env.get(k)]
    bases += [Path(env[k]) / "KiCad" for k in keys if env.get(k)]
    return [b / v / "bin" for b in bases for v in _VERSIONS]


def _posix_roots(env: Mapping[str, str]) -> list[Path]:
    roots = [Path("/usr/bin"), Path("/usr/local/bin"),
             Path("/Applications/KiCad/KiCad.app/Contents/MacOS")]
    if env.get("HOME"):
        roots.append(Path(env["HOME"]) / ".local" / "bin")
    return roots


def _locate(name: str, env_var: str, env: Mapping[str, str] | None) -> Path | None:
    env = os.environ if env is None else env
    override = env.get(env_var)
    if override:
        path = Path(override)
        return path if path.is_file() else None

    exe = f"{name}.exe" if os.name == "nt" else name
    found = shutil.which(exe, path=env.get("PATH"))
    if found:
        return Path(found)
    for directory in (_windows_roots(env) if os.name == "nt" else _posix_roots(env)):
        candidate = directory / exe
        if candidate.is_file():
            return candidate
    return None


def find_cli(env: Mapping[str, str] | None = None) -> Path | None:
    """`kicad-cli` 的路径，找不到返回 None。"""
    return _locate("kicad-cli", CLI_ENV, env)


def run(args: Sequence[str | Path], *, cwd: Path | None = None,
        timeout: int = TIMEOUT, check: bool = False) -> subprocess.CompletedProcess:
    """调用 `kicad-cli <args...>`。`check=True` 时非零退出码抛 KicadError。"""
    cli = find_cli()
    if cli is None:
        raise KicadError(
            "未找到 kicad-cli。请安装 KiCad（Windows: `winget install KiCad.KiCad`），"
            f"或用环境变量 {CLI_ENV} 指向 kicad-cli 可执行文件。")
    argv = [str(cli), *(str(a) for a in args)]
    proc = subprocess.run(
        argv, cwd=None if cwd is None else str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise KicadError(
            f"kicad-cli {' '.join(str(a) for a in args)} 失败（退出码 "
            f"{proc.returncode}）：\n{proc.stdout}\n{proc.stderr}".strip()
        )
    return proc


def erc(sch: Path) -> tuple[list[str], list[str]]:
    """跑原理图 ERC，返回 (错误列表, 警告列表)。

    用 --format json 拿结构化报告。**报告结构不符预期时抛 KicadError**——
    宁可失败也不静默返回「0 错误」的假通过（血的教训：官方符号没解析
    出来时 ERC 全是违规，旧版解析器却报告 0 错误 0 警告）。
    """
    proc = run(["sch", "erc", "--format", "json", "--output",
                str(sch.with_suffix(".erc.json")), str(sch)])
    report = sch.with_suffix(".erc.json")
    if proc.returncode != 0 or not report.exists():
        text = proc.stdout + "\n" + proc.stderr
        if text.strip():
            raise KicadError(f"ERC 执行失败（退出码 {proc.returncode}）：\n{text}")
        raise KicadError(f"ERC 执行失败且没有输出报告（退出码 {proc.returncode}）")

    data = json.loads(report.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    sheets = data.get("sheets")
    if isinstance(sheets, list):
        for sheet in sheets:
            for v in sheet.get("violations", []):
                sev = v.get("severity", "")
                desc = v.get("description", "?")
                items = v.get("items", [])
                for it in items:
                    where = it.get("description", "")
                    line = f"{desc}: {where}" if where else desc
                    if sev == "error":
                        errors.append(line)
                    elif sev == "warning":
                        warnings.append(line)
                    else:
                        raise KicadError(
                            f"ERC 报告出现未知严重级别 '{sev}'（{line}）")
        return errors, warnings
    raise KicadError(
        f"ERC 报告结构未知（顶层键: {list(data.keys())}），"
        f"请升级 backend/kicad/cli.py 的解析器——拒绝假通过。")


def export_netlist(sch: Path, out: Path) -> Path:
    """导出网表到 `out`（KiCad 传统 .net 格式），返回路径。"""
    run(["sch", "export", "netlist", "--output", str(out), str(sch)], check=True)
    return out
