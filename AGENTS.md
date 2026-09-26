# Agent instructions

- Never fake completion or hide errors.
- Fix root causes instead of bypassing requirements or tests.
- Verify changes before claiming success.
- State assumptions and unresolved validation clearly.
- Preserve user changes and historical evidence.
- Keep generated artifacts under `output/`; do not commit them.
- Do not commit, push, order hardware, or operate hardware without explicit authorization.
- Run `python -m unittest discover -s tests -t .` after code changes.
- Use `--profile schematic` for release claims; `auto` is interactive compatibility only.
- Treat `docs/HANDOFF_T07.md` as the current handoff and older handoffs as history.
