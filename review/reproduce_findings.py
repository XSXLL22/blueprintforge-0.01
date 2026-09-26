"""Read-only audit probes; run from any directory with Python 3.10+.

These report observed behavior, not passing regression tests. Mocked KiCad
responses test Python control flow only and do not substitute for real ERC.
Temporary generated artifacts are removed on exit; source files are unchanged.
"""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gen
from backend.kicad import cli
from backend.kicad.schematic import _net_of_map, render
from ir.schema import from_dict, load
from ir.validate import validate
from parts.partsdb import PARTS

BASE = json.loads((ROOT / 'examples/buck_12v_to_3v3.json').read_text(encoding='utf-8'))


def result(raw):
    outcome = validate(from_dict(raw))
    return {'ok': outcome.ok, 'codes': [d.code for d in outcome.diagnostics]}


def main():
    rows = []

    def record(name, expected, observed):
        rows.append({'probe': name, 'expected': expected, 'observed': observed})

    raw = copy.deepcopy(BASE)
    next(n for n in raw['nets'] if n['name'] == 'GND')['pins'].append({'ref': 'U1', 'pin': 'VIN'})
    record('duplicate_pin_across_nets', 'reject physical pin assigned to VIN and GND',
           {**result(raw), 'rendered_U1_pin6_net': _net_of_map(from_dict(raw))[('U1', '6')]})

    raw = copy.deepcopy(BASE)
    for n in raw['nets']:
        n['pins'] = [p for p in n['pins'] if p != {'ref': 'U1', 'pin': 'VIN'}]
    record('missing_required_vin', 'reject missing VIN connection',
           {**result(raw), 'generated_no_connect_count': render(from_dict(raw)).count('(no_connect ')})

    raw = copy.deepcopy(BASE)
    raw['components'][0]['footprint'] = 'Audit:RequestedFootprint'
    record('ignored_footprint_override', 'emit the explicitly requested footprint or reject it',
           {**result(raw), 'override_present': 'Audit:RequestedFootprint' in render(from_dict(raw))})

    raw = copy.deepcopy(BASE)
    raw['components'][0]['part'] = 'AUDIT_REGULATOR'
    parts = dict(PARTS)
    parts['AUDIT_REGULATOR'] = replace(PARTS['MP1584EN'], name='AUDIT_REGULATOR')
    record('injected_parts_disconnected', 'use supplied catalog for both symbols and connections',
           {'ok': validate(from_dict(raw), parts).ok,
            'generated_no_connect_count': render(from_dict(raw), parts).count('(no_connect ')})

    for value in ('NaN', 'Infinity'):
        raw = copy.deepcopy(BASE)
        raw['electrical']['iout_max'] = value
        record('nonfinite_current_' + value, 'reject non-finite current', result(raw))

    raw = copy.deepcopy(BASE)
    raw['electrical']['vin'] = {'min': 90, 'typ': 100, 'max': 110}
    raw['electrical']['iout_max'] = 99
    record('component_ratings_ignored', 'reject demand outside regulator/capacitor ratings', result(raw))

    raw = copy.deepcopy(BASE)
    raw['topology'] = 'sync_buck'
    record('regulator_topology_mismatch', 'reject async MP1584 used as sync_buck', result(raw))

    raw = copy.deepcopy(BASE)
    raw['components'] = [c for c in raw['components'] if c['ref'] != 'D1']
    for n in raw['nets']:
        n['pins'] = [p for p in n['pins'] if p['ref'] != 'D1']
    record('async_buck_without_diode', 'reject missing required freewheel diode', result(raw))

    raw = copy.deepcopy(BASE)
    raw['constraints']['ripple_target_mv'] = 'not-a-number'
    record('constraints_not_validated', 'reject malformed supported constraint', result(raw))

    with tempfile.TemporaryDirectory(prefix='circuitos-audit-') as directory:
        temp = Path(directory)
        raw = copy.deepcopy(BASE)
        raw['project'] = 123
        bad = temp / 'bad.json'
        bad.write_text(json.dumps(raw), encoding='utf-8')
        try:
            validate(load(bad))
            observed = 'accepted'
        except Exception as error:
            observed = type(error).__name__ + ': ' + str(error)
        record('malformed_project_type', 'structured input diagnostic', observed)

        sch = temp / 'audit.kicad_sch'
        for name, report in (
            ('erc_error_without_items', {'sheets': [{'violations': [{'severity': 'error', 'description': 'audit failure', 'items': []}]}]}),
            ('erc_missing_violations', {'sheets': [{}]}),
        ):
            sch.with_suffix('.erc.json').write_text(json.dumps(report), encoding='utf-8')
            with patch.object(cli, 'run', return_value=CompletedProcess([], 0, '', '')):
                errors, warnings = cli.erc(sch)
            record(name, 'retain error or reject incomplete report', {'errors': errors, 'warnings': warnings, 'mocked': True})

        capture = io.StringIO()
        wrong_net = temp / 'wrong.net'
        wrong_net.write_text('(export (nets))', encoding='utf-8')
        with patch.object(gen, 'find_cli', return_value=Path('mock-kicad-cli')), \
             patch.object(gen, 'erc', return_value=([], [])), \
             patch.object(gen, 'export_netlist', return_value=wrong_net), \
             redirect_stdout(capture):
            code = gen.main([str(ROOT / 'examples/buck_12v_to_3v3.json'), '-o', str(temp)])
        record('cli_no_netlist_reconciliation', 'reject exported netlist missing all IR nets',
               {'exit_code': code, 'reports_all_passed': '\u5168\u90e8\u901a\u8fc7' in capture.getvalue(), 'mocked': True})

    print(json.dumps(rows, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()
