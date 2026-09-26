"""Profile gates, output isolation, failure reports, and provenance contracts."""
from contextlib import ExitStack
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline import build as pb
from pipeline.models import REQUIRED
from pipeline.workspace import Workspace, digest
from parts.catalog import CatalogValidationPolicy
from parts.partsdb import PARTS
from diagnostics import Diagnostic
from backend.kicad.erc import ErcReport

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/buck_12v_to_3v3.json'


def pads(footprint):
    # Controlled reader for orchestration tests; electrical truth is tested separately.
    p = next(p for p in PARTS.values() if p.footprint == footprint)
    return tuple(SimpleNamespace(number=pin.number) for pin in p.pins)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / 'builds'

    def run_build(self, **kwargs):
        kwargs.setdefault('profile', 'generate')
        kwargs.setdefault('policy', CatalogValidationPolicy())
        return pb.build(EXAMPLE, self.out, **kwargs)

    def tool(self, version='10.0.6'):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(pb.cli, 'find_cli', return_value=Path('test-cli')))
        stack.enter_context(patch.object(pb.cli, 'run', return_value=
                           subprocess.CompletedProcess([], 0, version, '')))
        return stack

    def strict(self, **kwargs):
        kwargs.setdefault('policy', CatalogValidationPolicy(pads))
        return self.run_build(profile='schematic', **kwargs)

    def saved(self, report):
        self.assertIsNotNone(report.report_path)
        return json.loads(Path(report.report_path).read_text(encoding='utf-8'))

    def test_generate_without_tools_retains_all_targets(self):
        with patch.object(pb.cli, 'run', side_effect=AssertionError('must not call KiCad')):
            r = self.run_build()
        self.assertTrue(r.passed_profile, r.to_dict())
        self.assertEqual(len(r.pending_targets), 4)
        self.assertFalse(r.verified_targets)
        self.assertEqual(r.stages['erc'].status, 'skipped')
        self.assertEqual(len(r.unverified), 22)
        self.assertTrue(self.saved(r)['passed_profile'])

    def test_schematic_missing_tool_cannot_downgrade(self):
        with patch.object(pb.cli, 'find_cli', return_value=None):
            r = self.strict()
        self.assertEqual(r.exit_code, 3)
        self.assertEqual(r.profile, 'schematic')
        self.assertEqual(r.stages['generate'].reason, 'prerequisite_failed')
        self.assertFalse(self.saved(r)['passed_profile'])

    def test_auto_records_actual_selection(self):
        with patch.object(pb.cli, 'find_cli', return_value=None):
            r = self.run_build(profile='auto')
        self.assertEqual((r.requested_profile, r.profile), ('auto', 'generate'))

    def test_unsupported_version_stops(self):
        self.tool('99.0.0')
        r = self.strict()
        self.assertEqual(r.exit_code, 3)
        self.assertEqual(r.stages['tool'].status, 'unsupported')

    def test_strict_cannot_weaken_reader_policy(self):
        self.tool()
        r = self.strict(policy=CatalogValidationPolicy())
        self.assertEqual(r.exit_code, 3)
        self.assertIn('E-CAT-006', {d['code'] for d in r.diagnostics})
        self.assertFalse(list(Path(r.report_path).parent.glob('*.kicad_sch')))

    def test_ep10_stops_real_pipeline_before_generation(self):
        p = PARTS['MP1584EN']
        broken = replace(p, pins=tuple(replace(pin, number='10') if pin.name == 'EP'
                                       else pin for pin in p.pins))
        r = self.run_build(parts={**PARTS, p.name: broken}, policy=CatalogValidationPolicy(pads))
        self.assertEqual(r.exit_code, 1)
        self.assertIn('E-CAT-002', {d['code'] for d in r.diagnostics})
        self.assertFalse(list(Path(r.report_path).parent.glob('*.kicad_sch')))

    def test_decode_failure_has_report_and_no_schematic(self):
        source = Path(self.tmp.name) / 'bad.json'
        source.write_text('{', encoding='utf-8')
        r = pb.build(source, self.out, profile='generate')
        self.assertEqual(r.exit_code, 1)
        self.assertEqual(self.saved(r)['stages']['input']['status'], 'fail')
        self.assertFalse(list(Path(r.report_path).parent.glob('*.kicad_sch')))

    def test_bad_design_is_not_bypassed_by_generate(self):
        raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        raw['electrical']['iout_max'] = -1
        source = Path(self.tmp.name) / 'bad.json'
        source.write_text(json.dumps(raw), encoding='utf-8')
        r = pb.build(source, self.out, profile='generate', policy=CatalogValidationPolicy())
        self.assertEqual(r.exit_code, 1)
        self.assertFalse(r.passed_profile)

    def test_repeat_runs_isolated_and_schematic_deterministic(self):
        a, b = self.run_build(), self.run_build()
        pa, pb_ = Path(a.report_path).parent, Path(b.report_path).parent
        self.assertNotEqual(pa, pb_)
        self.assertEqual(next(pa.glob('*.kicad_sch')).read_bytes(),
                         next(pb_.glob('*.kicad_sch')).read_bytes())
        self.assertEqual(a.input['normalized_sha256'], b.input['normalized_sha256'])

    def test_manifest_all_hashes_match_and_report_identifies_catalog(self):
        r = self.run_build()
        directory = Path(r.report_path).parent
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['disposition'], 'deliverable')
        for entry in manifest['artifacts']:
            self.assertEqual(digest((directory / entry['path']).read_bytes()), entry['sha256'])
        self.assertEqual(self.saved(r)['catalog']['revision'], '9a6bc694906b648b')
        self.assertIn('checks/subgraph.py', r.provenance['source_files'])

    def test_output_path_is_file_returns_io_failure(self):
        self.out.write_text('occupied', encoding='utf-8')
        r = self.run_build()
        self.assertEqual(r.exit_code, 4)
        self.assertIsNone(r.report_path)
        self.assertFalse(r.passed_profile)

    def test_publication_failure_never_leaves_passed_directory(self):
        with patch.object(Path, 'rename', side_effect=PermissionError('denied')):
            r = self.run_build()
        self.assertEqual(r.exit_code, 4)
        self.assertFalse(list(self.out.glob('passed-*')))
        self.assertFalse(self.saved(r)['passed_profile'])

    def test_internal_generation_error_retains_failure_report(self):
        with patch.object(pb, 'write_resolved', side_effect=RuntimeError('broken renderer')):
            r = self.run_build()
        self.assertEqual(r.exit_code, 4)
        self.assertEqual(r.stages['generate'].status, 'error')
        manifest = json.loads((Path(r.report_path).parent / 'manifest.json').read_text())
        self.assertEqual(manifest['disposition'], 'diagnostic_only')

    def test_missing_input_returns_io_report(self):
        r = pb.build(Path(self.tmp.name) / 'missing.json', self.out, profile='generate')
        self.assertEqual(r.exit_code, 4)
        self.assertFalse(self.saved(r)['passed_profile'])

    def test_tool_timeout_is_error(self):
        self.tool()
        with patch.object(pb.cli, 'run', side_effect=subprocess.TimeoutExpired('cli', 1)):
            r = self.strict()
        self.assertEqual(r.exit_code, 3)
        self.assertEqual(r.stages['tool'].status, 'error')

    def test_invalid_erc_stops_netlist(self):
        self.tool()
        with patch.object(pb.erc, 'verify', return_value=(None, [])), \
             patch.object(pb.cli, 'export_netlist') as exporter:
            r = self.strict()
        self.assertEqual(r.exit_code, 3)
        exporter.assert_not_called()

    def test_warning_and_disabled_critical_checks_block_schematic(self):
        self.tool()
        for code in ('W-ERC-002', 'W-ERC-003', 'E-ERC-004'):
            with self.subTest(code=code), patch.object(pb.erc, 'verify', return_value=(
                    ErcReport(Path('mock')), [Diagnostic(code,
                    'error' if code.startswith('E') else 'warning', '', 'test')])):
                r = self.strict()
            self.assertEqual(r.exit_code, 2)
            self.assertEqual(r.stages['netlist'].reason, 'prerequisite_failed')

    def test_missing_export_file_cannot_reuse_previous_build(self):
        self.tool()
        with patch.object(pb.erc, 'verify', return_value=(ErcReport(Path('mock')), [])), \
             patch.object(pb.cli, 'export_netlist', side_effect=lambda sch, out: out):
            r = self.strict()
        self.assertEqual(r.exit_code, 3)
        self.assertEqual(r.stages['netlist'].reason, 'missing_or_foreign_netlist')

    def test_success_requires_all_required_stages(self):
        r = self.run_build()
        for name in REQUIRED['generate']:
            old = r.stages[name].status
            r.stages[name].status = 'skipped'
            self.assertFalse(r.passed_profile, name)
            r.stages[name].status = old

    def test_resolve_called_once_per_build(self):
        from backend.kicad import schematic
        with patch.object(pb, 'resolve', wraps=pb.resolve) as resolver, \
             patch.object(schematic, 'resolve', side_effect=AssertionError('duplicate resolution')):
            r = self.run_build()
        self.assertTrue(r.passed_profile)
        self.assertEqual(resolver.call_count, 1)

    def test_cli_io_error_reports_on_stderr(self):
        import gen
        self.out.write_text('occupied', encoding='utf-8')
        with patch('sys.stderr', new_callable=io.StringIO) as err, \
             patch('sys.stdout', new_callable=io.StringIO):
            code = gen.main([str(EXAMPLE), '-o', str(self.out), '--profile', 'generate'])
        self.assertEqual(code, 4)
        self.assertIn('"passed_profile": false', err.getvalue())
