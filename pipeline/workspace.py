"""Isolated builds; directory rename publishes report and artifacts together."""
import hashlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
                    + '\n', encoding='utf-8')


class Workspace:
    def __init__(self, root: Path, build_id: str):
        self.root = root.resolve()
        self.staging = self.root / ('.staging-' + build_id)
        self.staging.mkdir(parents=True, exist_ok=False)
        self.build_id = build_id

    def inventory(self) -> list[dict]:
        return [{'path': p.relative_to(self.staging).as_posix(),
                 'sha256': digest(p.read_bytes()), 'size_bytes': p.stat().st_size}
                for p in sorted(self.staging.rglob('*')) if p.is_file()
                and p.name not in ('build-report.json', 'manifest.json')]

    def finish(self, report, success: bool) -> Path:
        destination = self.root / (('passed-' if success else 'failed-') + self.build_id)
        report.report_path = str(destination / 'build-report.json')
        report.artifacts = self.inventory()
        write_json(self.staging / 'build-report.json', report.to_dict())
        # Manifest hashes the report too; no self-referential manifest hash.
        entries = report.artifacts + [{'path': 'build-report.json',
            'sha256': digest((self.staging / 'build-report.json').read_bytes()),
            'size_bytes': (self.staging / 'build-report.json').stat().st_size}]
        write_json(self.staging / 'manifest.json', {
            'schema_version': '1.0', 'build_id': report.build_id,
            'disposition': 'deliverable' if success else 'diagnostic_only',
            'passed_profile': report.passed_profile, 'artifacts': entries})
        self.staging.rename(destination)
        return destination
