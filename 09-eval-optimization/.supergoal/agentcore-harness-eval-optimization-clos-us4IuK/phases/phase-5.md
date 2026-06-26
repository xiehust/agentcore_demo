SUPERGOAL_PHASE_START
Phase: 5 of 6 — Optimization closed loop
Task: Turn evaluation findings into an improved agent via AgentCore Optimization and prove the improvement with a re-evaluation.
Type: real-cloud, ai-agents, optimization
Mandatory commands: uv run python scripts/run_optimization.py, uv run python scripts/compare.py, uv run python scripts/ab_test.py, uv run ruff check .
Acceptance criteria: 6
Evidence required: optimized prompt + rationale, results/comparison.json, compare table, ab_test output
Depends on phases: 1, 2, 3, 4

## Why

Demonstrate the observe->evaluate->improve loop end to end: convert evaluation findings into an improved agent and prove the gain with a fair re-evaluation.

## Work

- `scripts/run_optimization.py`: call the AgentCore **Recommendations** API against the agent's traces + baseline eval (target the goal-success evaluator) to produce an optimized system prompt / tool descriptions. Save the recommendation + rationale to `results/recommendation.json` and write the new prompt into `agent/prompts.py` as `OPTIMIZED_PROMPT`. If `optimization_api` is absent in config.json, generate an improved prompt via a documented Bedrock-judge fallback (ask a Claude model to rewrite the weak baseline given the eval failures) and record that this path was used.
- Apply the improved prompt to the running agent: prefer a **config-bundle swap** if available (the documented optimization path — no redeploy); else re-deploy the agent with `OPTIMIZED_PROMPT` (reuse phase-3 deploy path).
- Re-run `scripts/generate_sessions.py` for a fresh session set (same size as baseline) and `scripts/run_evaluation.py` -> `results/improved_scores.json` (same evaluators as baseline).
- `scripts/compare.py`: print a baseline-vs-improved table and write `results/comparison.json` containing both score sets, a `loop_closed` boolean, and a one-paragraph verdict.
- `scripts/ab_test.py`: a runnable A/B-test scaffold (config-bundle variants or target-based variants via Gateway) with a runbook comment. If the A/B API is present, attempt a real setup (control=baseline bundle, treatment=optimized bundle, online evaluator) and print the created config; else print the exact documented call sequence and exit 0 (degraded).
- Run `uv run ruff check .`.

## Acceptance criteria (all must pass — verify each in transcript)

- `uv run python scripts/run_optimization.py` produces an optimized prompt saved to `results/recommendation.json` and updates `OPTIMIZED_PROMPT` in `agent/prompts.py` (or uses the documented Bedrock-judge fallback and records that path).
- The improved agent is re-deployed/re-configured and re-evaluated; `results/improved_scores.json` exists with numeric aggregate scores for the same evaluators as baseline.
- `uv run python scripts/compare.py` prints a baseline-vs-improved comparison and writes `results/comparison.json` with both score sets + a `loop_closed` boolean + verdict.
- The comparison shows improved >= baseline on the primary evaluator, OR `comparison.json` contains an honest analysis of why not (no silent papering-over).
- `uv run python scripts/ab_test.py` exits 0 (real setup or documented degraded mode).
- `uv run ruff check .` exits 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv run python scripts/run_optimization.py`
- `uv run python scripts/compare.py`
- `uv run python scripts/ab_test.py`
- `uv run ruff check .`

## Evidence required in transcript

- `run_optimization.py` output: the optimized prompt + rationale (or documented fallback path).
- `results/comparison.json` contents (baseline vs improved + verdict).
- `compare.py` printed table.
- `ab_test.py` output (real or degraded).

## Notes

Re-query AWS Knowledge MCP for the exact Recommendations + config-bundle op names if the boto3 shapes are unclear. Keep the new session set the same size as baseline for a fair comparison. The baseline prompt was deliberately weak (phase 2), so a real improvement is expected; if scores don't move, report it honestly with analysis rather than forcing a pass.
