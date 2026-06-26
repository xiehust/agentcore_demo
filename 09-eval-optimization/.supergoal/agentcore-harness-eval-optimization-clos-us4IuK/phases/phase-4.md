SUPERGOAL_PHASE_START
Phase: 4 of 6 — Generate sessions + baseline evaluation
Task: Generate real agent sessions and score them with AgentCore Evaluations to establish the baseline.
Type: real-cloud, ai-agents, evaluation
Mandatory commands: uv run python scripts/generate_sessions.py, uv run python scripts/run_evaluation.py, uv run ruff check .
Acceptance criteria: 5
Evidence required: session ids created, eval job id + final state + aggregate scores, results/baseline_scores.json
Depends on phases: 1, 2, 3

## Why

Produce real agent sessions and score them with AgentCore Evaluations to establish the baseline the optimization loop must improve on.

## Work

- `dataset/eval_prompts.json`: ~10 customer-support prompts spanning order lookups, returns, shipping delays, discount requests, and multi-tool interactions, each with a short note on expected behavior (used as evaluation context / human-readable ground truth).
- `scripts/generate_sessions.py`: read the deployed runtime ARN from `deployment.json`; invoke the agent once per dataset prompt with a unique >=33-char session id; print one line per session (`id ok`). After the last invoke, wait ~2-3 min for CloudWatch ingestion (bounded sleep with progress prints).
- `scripts/run_evaluation.py`: start an AgentCore **batch evaluation** over the recent sessions using built-in evaluators `Builtin.GoalSuccessRate`, `Builtin.Helpfulness`, `Builtin.Faithfulness`. Resolve log group + service name from `deployment.json` (or `agentcore status --json`). Prefer the boto3 batch-evaluation op if `evaluation_api` is present; else use `agentcore run batch-evaluation --evaluator ... --wait` and parse output (record which path). Poll to a terminal state, then write aggregate + per-session scores to `results/baseline_scores.json`. If "no sessions found", wait + retry ingestion (bounded) before failing; always surface the terminal state.
- Run `uv run ruff check .`.

## Acceptance criteria (all must pass — verify each in transcript)

- `uv run python scripts/generate_sessions.py` invokes the agent for every dataset prompt and prints one line per session (id + ok).
- `uv run python scripts/run_evaluation.py` starts a batch evaluation and polls until a terminal state, printing the job id + final state.
- `results/baseline_scores.json` exists and contains a numeric aggregate score for each requested evaluator.
- `run_evaluation.py` handles "no sessions yet" with a bounded ingestion retry before failing, and surfaces the terminal state explicitly.
- `uv run ruff check .` exits 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv run python scripts/generate_sessions.py`
- `uv run python scripts/run_evaluation.py`
- `uv run ruff check .`

## Evidence required in transcript

- `generate_sessions.py` output (session ids created).
- `run_evaluation.py` output: job id, final state, aggregate scores.
- `results/baseline_scores.json` contents.

## Notes

CloudWatch ingestion lag is real — allow 2-3 min before the eval job and retry on empty. Keep the evaluator set small to bound cost. If an evaluator name differs in the installed API, re-query AWS Knowledge MCP for the current built-in evaluator identifiers and record them. Make both scripts idempotent/re-runnable.
