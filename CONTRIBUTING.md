# Contributing

CircuitOS is evidence driven: a green command is useful only when its validation scope is clear.

## Development setup

Python 3.11 or newer is recommended. The core project has no third-party Python dependencies.

```bash
python -m unittest discover -s tests -t .
python gen.py examples/buck_12v_to_3v3.json --profile generate -o output
```

The generate profile works without KiCad and is used by public CI. Integration claims require
KiCad 10.0.6 and an explicit schematic profile:

```bash
python gen.py examples/buck_12v_to_3v3.json --profile schematic -o output
python -m tests.t06_acceptance
```

## Change rules

- Add a meaningful negative test for a bug before or with its fix.
- Keep electrical semantics in `ir/`, `checks/`, `parts/`, or `backend/`; do not copy them into
  the pipeline or CLI.
- Preserve stable diagnostic codes. Register a new code before using it.
- Missing tools, unreadable reports and skipped checks must remain visible.
- Do not lower user targets, add invented evidence, suppress warnings, or weaken a profile to
  make tests pass.
- Put generated evidence under `output/`, which is intentionally ignored by Git.
- Update README/DESIGN and the current handoff when a public contract changes.

Before opening a pull request, run `git diff --check`, the full test suite, and at least the
generate profile. Include the exact commands, pass/fail/skip counts, KiCad version if used, and
remaining limitations in the pull request description.
