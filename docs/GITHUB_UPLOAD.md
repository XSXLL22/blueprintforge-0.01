# GitHub upload checklist

The repository is configured for `https://github.com/XSXLL22/blueprintforge-0.01.git` on the
`main` branch. Generated builds, local caches, editor state and environment files are ignored.

## Before the first upload

Review the complete change set because all T00–T07 work is currently uncommitted:

```powershell
git status --short
git diff --check
git diff -- README.md DESIGN.md
```

Run the public, tool-free checks:

```powershell
$env:COS_KICAD_CLI = 'Z:\path-that-does-not-exist\kicad-cli.exe'
python -m unittest discover -s tests -t .
python gen.py examples/buck_12v_to_3v3.json --profile generate -o output/github-check
```

For an integration release claim, also run KiCad 10.0.6 locally:

```powershell
$env:COS_KICAD_CLI = 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe'
python -m tests.t06_acceptance
python gen.py examples/buck_12v_to_3v3.json --profile schematic -o output/github-check
```

## Commit and push

Only run these commands after reviewing the changes:

```powershell
git add .
git diff --cached --check
git status --short
git commit -m "Add verified IR-to-KiCad validation pipeline"
git push origin main
```

The `output/` evidence directory is intentionally excluded. GitHub Actions will reproduce the
software tests and generate profile on Python 3.11, 3.12 and 3.13. Local KiCad integration
evidence remains documented in `docs/HANDOFF_T07.md`.

## Recommended repository settings

- Keep the repository private until the first Actions run succeeds if you want to review its
  public presentation before release.
- Enable branch protection after the first successful CI run and require the `test` checks.
- Enable GitHub secret scanning and push protection.
- Add a short repository description such as: “Deterministic circuit IR to verified KiCad
  schematics with explicit validation scope.”
- Do not create a release that claims PCB or performance validation; those stages remain pending.
