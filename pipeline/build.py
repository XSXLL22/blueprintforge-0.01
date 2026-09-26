"""One resolved snapshot, explicit stage gates, isolated build reports."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid

from backend.kicad import cli, erc, netlist
from backend.kicad.footprint import electrical_pads, footprints_root
from backend.kicad.project import write_project
from backend.kicad.schematic import write_resolved
from diagnostics import Diagnostic
from ir.decode import decode_text
from ir.resolved import as_catalog, resolve
from ir.validate import validate_resolved
from parts.catalog import CatalogValidationPolicy
from pipeline.models import BuildReport, REQUIRED, Stage
from pipeline.workspace import Workspace, digest, write_json

# Only versions backed by real integration evidence, not presumed compatibility.
SUPPORTED_KICAD = ('10.0.6',)
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


class StopBuild(Exception):
    def __init__(self, code):
        self.code = code


def catalog_policy(profile='generate'):
    """Discovery chooses a reader; the requested profile determines strictness."""
    return CatalogValidationPolicy(
        pad_provider=electrical_pads if footprints_root() is not None else None,
        require_pads=profile == 'schematic')


def provenance():
    files = [ROOT / 'gen.py', ROOT / 'diagnostics.py']
    for folder in ('pipeline', 'ir', 'parts', 'checks', 'backend/kicad'):
        files += list((ROOT / folder).rglob('*.py'))
    files += list((ROOT / 'backend/kicad/symbols').glob('*.kicad_sym'))
    files.append(Path(erc.ALLOWLIST))
    hashes = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
              for p in sorted(set(files))}
    return {'source_sha256': digest(json.dumps(hashes, sort_keys=True).encode()),
            'source_files': hashes}


def build(input_path: Path, output_root: Path, *, profile='auto', parts=None,
          policy: CatalogValidationPolicy | None = None) -> BuildReport:
    """Return a report on every failure. Injectable readers cannot weaken schematic."""
    report = BuildReport(uuid.uuid4().hex, profile, profile, started_at=now())
    ws, current = None, 'input'

    def problem(code, message, stage=None):
        report.diagnostics.append(Diagnostic(code, 'error', str(input_path), message,
                                  stage=stage or current).to_dict())

    def stop(stage, status, reason, code, details=None):
        report.stages[stage] = Stage(status, reason, details or {})
        raise StopBuild(code)

    try:
        ws = Workspace(output_root, report.build_id)
        report.provenance = provenance()
        if profile == 'auto':
            profile = 'schematic' if cli.find_cli() else 'generate'
        report.profile = profile
        if profile not in REQUIRED:
            problem('E-BUILD-001', f'未知 profile: {profile}')
            stop('input', 'fail', 'unknown_profile', 1)
        if profile == 'generate':
            for name in ('tool', 'erc', 'netlist'):
                report.stages[name] = Stage('skipped', 'profile_not_requested')
            report.scope = '仅生成；ERC、外部网表、PCB、性能、仿真、实测和报价未验证'
        raw = input_path.read_bytes()
        report.input = {'path': str(input_path.resolve()), 'sha256': digest(raw)}
        (ws.staging / 'input.json').write_bytes(raw)
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeError as exc:
            problem('E-IR-DECODE-010', f'输入不是有效 UTF-8: {exc}')
            stop('input', 'fail', 'invalid_encoding', 1)
        decoded = decode_text(text, source=str(input_path))
        report.diagnostics.extend(d.to_dict() for d in decoded.diagnostics)
        report.migrations = list(decoded.migrations)
        if not decoded.usable:
            stop('input', 'fail', 'invalid_input', 1)
        ir = decoded.draft
        normalized = asdict(ir)
        encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        report.input['normalized_sha256'] = digest(encoded)
        write_json(ws.staging / 'normalized-ir.json', normalized)
        report.pending_targets = [{**asdict(t), 'status': 'unsupported',
            'reason': 'target_evaluator_not_implemented'} for t in ir.targets]
        report.stages['input'] = Stage('pass', 'decoded')

        if profile == 'schematic':
            current = 'tool'
            executable = cli.find_cli()
            report.tools = {'kicad_cli': str(executable) if executable else None,
                            'supported_versions': list(SUPPORTED_KICAD)}
            if executable is None:
                problem('E-BUILD-004', 'schematic 必须安装 KiCad，不能自动降级')
                stop('tool', 'error', 'tool_missing', 3)
            proc = cli.run(['--version'], timeout=30)
            version = proc.stdout.strip()
            report.tools['kicad_version'] = version
            if proc.returncode or not version:
                problem('E-BUILD-004', f'KiCad 版本查询失败: {proc.stderr}')
                stop('tool', 'error', 'version_query_failed', 3)
            if version not in SUPPORTED_KICAD:
                problem('E-BUILD-004', f'未验证的 KiCad 版本: {version}')
                stop('tool', 'unsupported', 'unsupported_version', 3)
            report.stages['tool'] = Stage('pass', 'supported_version')

        current = 'catalog'
        catalog = as_catalog(parts)
        report.catalog = {'id': catalog.id, 'name': catalog.name,
            'revision': catalog.revision, 'supply_revision': catalog.supply_revision}
        write_json(ws.staging / 'catalog-electrical.json', catalog.electrical_dict())
        chosen = policy if policy is not None else catalog_policy(profile)
        if profile == 'schematic':
            chosen = CatalogValidationPolicy(chosen.pad_provider, require_pads=True)
        rir = resolve(ir, catalog, catalog_policy=chosen)
        report.unverified = list(rir.unverified + rir.catalog_unverified)
        cat_diags = [d for d in rir.diagnostics if '-CAT-' in d.code]
        report.diagnostics.extend(d.to_dict() for d in cat_diags)
        errors = [d for d in cat_diags if d.is_error]
        if errors:
            unreadable = any(d.code == 'E-CAT-006' for d in errors)
            stop('catalog', 'error' if unreadable else 'fail',
                 'footprint_unavailable' if unreadable else 'invalid_catalog',
                 3 if unreadable else 1)
        report.stages['catalog'] = Stage('pass', 'required_checks_passed',
            {'require_pads': chosen.require_pads, 'unchecked': list(rir.catalog_unverified)})
        current = 'resolve'
        outcome = validate_resolved(rir)
        report.diagnostics.extend(d.to_dict() for d in outcome.diagnostics
                                  if '-CAT-' not in d.code)
        if not outcome.ok:
            stop('resolve', 'fail', 'invalid_design', 1)
        report.stages['resolve'] = Stage('pass', 'supported_design_rules_passed')

        current = 'generate'
        sch = write_resolved(rir, ws.staging)
        pro = write_project(sch)
        required = [sch, pro, ws.staging / 'circuitos.kicad_sym', ws.staging / 'sym-lib-table']
        if not all(p.is_file() and p.stat().st_size for p in required):
            problem('E-BUILD-003', '生成器未产出全部必需文件')
            stop('generate', 'error', 'missing_artifacts', 4)
        report.stages['generate'] = Stage('pass', 'artifacts_exist')
        if profile == 'schematic':
            current = 'erc'
            result, diags = erc.verify(sch, cli.erc_report_path(sch))
            report.diagnostics.extend(d.to_dict() for d in diags)
            details = {} if result is None else {
                'errors': len(result.errors), 'warnings': len(result.warnings),
                'ignored_checks': list(result.ignored_checks), 'kicad_version': result.kicad_version}
            if result is None or any(d.code in ('E-ERC-001', 'E-ERC-002', 'E-ERC-003',
                                                'E-ERC-005') for d in diags):
                if result is None and not any(d.is_error for d in diags):
                    problem('E-BUILD-004', 'ERC 未返回有效报告')
                stop('erc', 'error', 'erc_unavailable', 3, details)
            if any(d.is_error for d in diags):
                stop('erc', 'fail', 'erc_violations', 2, details)
            if any(d.code in ('W-ERC-002', 'W-ERC-003') for d in diags):
                problem('E-BUILD-005', '存在未豁免 ERC 告警或重要检查被禁用')
                stop('erc', 'fail', 'erc_policy_failed', 2, details)
            report.stages['erc'] = Stage('pass', 'erc_gate_passed', details)
            current = 'netlist'
            expected = ws.staging / f'{ir.project}.xml'
            exported = cli.export_netlist(sch, expected)
            if exported.resolve() != expected.resolve() or not expected.is_file():
                problem('E-BUILD-004', '网表导出未返回本次构建的新文件')
                stop('netlist', 'error', 'missing_or_foreign_netlist', 3)
            parsed, diags = netlist.read_netlist(expected)
            if parsed is None:
                report.diagnostics.extend(d.to_dict() for d in diags)
                stop('netlist', 'error', 'invalid_netlist_report', 3)
            diags += netlist.reconcile_resolved(rir, parsed,
                aux_refs=netlist.auxiliary_refs(sch.read_text(encoding='utf-8')))
            report.diagnostics.extend(d.to_dict() for d in diags)
            details = {'nets': len(parsed.nets), 'components': len(parsed.components)}
            if any(d.is_error for d in diags):
                stop('netlist', 'fail', 'netlist_mismatch', 2, details)
            report.stages['netlist'] = Stage('pass', 'pinwise_match', details)
        report.exit_code = 0
    except StopBuild as exc:
        report.exit_code = exc.code
    except (cli.KicadError, subprocess.TimeoutExpired) as exc:
        problem('E-BUILD-004', f'工具执行失败: {exc}')
        report.stages[current] = Stage('error', 'tool_execution_failed')
        report.exit_code = 3
    except OSError as exc:
        tool_error = current == 'tool'
        problem('E-BUILD-004' if tool_error else 'E-BUILD-003', f'I/O 失败: {exc}')
        report.stages[current if ws else 'publish'] = Stage('error',
            'tool_execution_failed' if tool_error else 'io_error')
        report.exit_code = 3 if tool_error else 4
    except Exception as exc:
        problem('E-BUILD-006', f'{type(exc).__name__}: {exc}')
        report.stages[current] = Stage('error', 'internal_error')
        report.exit_code = 4

    report.finished_at = now()
    if ws is not None:
        success = report.exit_code == 0
        report.stages['publish'] = (Stage('pass', 'profile_artifacts_published')
                                   if success else Stage('skipped', 'prerequisite_failed'))
        try:
            ws.finish(report, success)
        except Exception as exc:
            report.exit_code = 4
            report.report_path = None
            report.stages['publish'] = Stage('error', 'publication_failed')
            problem('E-BUILD-003', f'报告/manifest/目录发布失败: {exc}', 'publish')
            # Replace optimistic staging metadata; never expose it as passed-*.
            try:
                write_json(ws.staging / 'manifest.json', {
                    'schema_version': '1.0', 'disposition': 'diagnostic_only',
                    'passed_profile': False, 'artifacts': []})
                report.report_path = str(ws.staging / 'build-report.json')
                write_json(ws.staging / 'build-report.json', report.to_dict())
            except OSError:
                report.report_path = None
    return report
