"""Versioned report contract. No implicit success from absent stages."""
from dataclasses import asdict, dataclass, field
from typing import Literal

Status = Literal['pass', 'fail', 'skipped', 'unsupported', 'error']
BASE = ('input', 'catalog', 'resolve', 'generate', 'publish')
REQUIRED = {'generate': BASE, 'schematic': BASE + ('tool', 'erc', 'netlist')}
STAGES = ('input', 'tool', 'catalog', 'resolve', 'generate', 'erc', 'netlist', 'publish')


@dataclass
class Stage:
    status: Status = 'skipped'
    reason: str = 'prerequisite_failed'
    details: dict = field(default_factory=dict)


@dataclass
class BuildReport:
    build_id: str
    requested_profile: str
    profile: str
    schema_version: str = '1.0'
    stages: dict[str, Stage] = field(default_factory=lambda: {s: Stage() for s in STAGES})
    diagnostics: list[dict] = field(default_factory=list)
    input: dict = field(default_factory=dict)
    catalog: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    pending_targets: list[dict] = field(default_factory=list)
    verified_targets: list[dict] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    migrations: list[str] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    started_at: str = ''
    finished_at: str = ''
    report_path: str | None = None
    exit_code: int = 4
    scope: str = 'PCB、性能、仿真、实测和报价未验证'

    @property
    def passed_profile(self) -> bool:
        return (self.exit_code == 0 and self.profile in REQUIRED
                and all(self.stages[s].status == 'pass' for s in REQUIRED[self.profile])
                and not any(d['severity'] == 'error' for d in self.diagnostics))

    def to_dict(self) -> dict:
        return {**asdict(self), 'passed_profile': self.passed_profile}
