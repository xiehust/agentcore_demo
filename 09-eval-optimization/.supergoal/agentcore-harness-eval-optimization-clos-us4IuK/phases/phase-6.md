SUPERGOAL_PHASE_START
Phase: 6 of 6 — Docs, diagram, teardown & Polish/Harden
Task: Make the demo reproducible and understandable, bound ongoing cost, and verify every aspect.
Type: docs, polish, real-cloud
Mandatory commands: uv run python -m py_compile agent/*.py scripts/*.py, uv run pytest -q, uv run ruff check .
Acceptance criteria: 8
Evidence required: per-sub-pass paragraph, docs/diagram ls + README excerpt, teardown dry-run, git diff --stat + final checks
Depends on phases: 1, 2, 3, 4, 5

## Why

Catch what shipping-focused phases missed, make the demo reproducible + understandable, and bound ongoing cost. This is how "every aspect is perfect" gets enforced.

## Work (sub-passes — each must produce evidence)

- **Docs/runbook**: complete `README.md` — prereqs, the one-command-per-step reproduce path (preflight -> build -> deploy -> generate+evaluate -> optimize -> teardown), a cost estimate, and a troubleshooting section (model access, Transaction Search, ingestion lag, absent-API degradations).
- **Concepts**: `docs/CONCEPTS.md` — explain AgentCore **Harness**, **Evaluations**, **Optimization** with the real API/CLI mapping (CreateHarness/InvokeHarness; batch evaluation + built-in evaluators; Recommendations/config-bundles/A-B), and exactly how each demo script exercises them.
- **Diagram**: render the closed-loop architecture (build -> deploy -> observe -> evaluate -> optimize -> redeploy) as SVG + PNG via the `fireworks-tech-graph` skill; reference it from README.
- **Teardown**: `scripts/teardown.py` that deletes resources the demo created (runtime, harness if any, eval/optimization artifacts) reading `deployment.json`; dry-run by default, `--yes` to execute; documented in README.
- **States/edges**: confirm every script guards missing `config.json`/`deployment.json`, absent sessions, and absent APIs with clear messages (read each script's guards and note them).
- **Security**: no AWS creds/secrets committed; `.gitignore` covers `.venv/`, caches, and anything sensitive; add a least-privilege note to README.
- **Diff review**: scan the final diff for stray debug prints / leftover TODOs from this run; remove them.
- **Regression sweep**: compile all modules, run pytest, run ruff — all green.

## Acceptance criteria (all must pass — verify each in transcript)

- `README.md` contains the full step-by-step reproduce runbook + cost estimate + troubleshooting.
- `docs/CONCEPTS.md` explains Harness, Evaluations, and Optimization with the real API/CLI mapping.
- A closed-loop architecture diagram exists as both `.svg` and `.png` and is referenced from README.
- `scripts/teardown.py` exists, defaults to dry-run, lists target resources from `deployment.json`, and is documented in README.
- Every script guards missing config/deployment/sessions/absent-API with a clear message (verified by reading the guards).
- No secrets committed; `.gitignore` covers `.venv/`, caches, and sensitive files.
- Final `git diff --stat` reviewed; no stray debug/TODO from this run.
- `uv run python -m py_compile agent/*.py scripts/*.py`, `uv run pytest -q`, and `uv run ruff check .` all exit 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv run python -m py_compile agent/*.py scripts/*.py`
- `uv run pytest -q`
- `uv run ruff check .`

## Evidence required in transcript

- One paragraph per sub-pass with what was checked / found / fixed.
- `ls docs/` showing CONCEPTS.md + diagram (SVG + PNG) and a README excerpt showing the reproduce runbook.
- `teardown.py` dry-run output listing target resources.
- Final `git diff --stat` summary + final compile/test/lint summary.

## Notes

Invoke the `fireworks-tech-graph` skill for the diagram. teardown.py must be safe by default (dry-run); never delete without `--yes`. If any earlier phase ran in a degraded mode (absent API), make sure README + CONCEPTS reflect that honestly. Write the final `project_*` memory (location, stack, status, ROADMAP link, AgentCore API surface learned) per PROTOCOL memory writeback.
