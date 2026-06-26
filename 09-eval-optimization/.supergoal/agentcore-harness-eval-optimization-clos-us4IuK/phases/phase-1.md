SUPERGOAL_PHASE_START
Phase: 1 of 6 — Preflight & scaffold
Task: Stand up a uv Python project + AgentCore CLI and verify all real-cloud prerequisites before spending money/time.
Type: greenfield, real-cloud, ai-agents
Mandatory commands: uv sync, uv run python preflight.py, uv run ruff check .
Acceptance criteria: 6
Evidence required: preflight report tail, config.json, agentcore --version
Depends on phases: none

## Why

Fail fast on missing prerequisites or SDK/CLI gaps and lock the toolchain before any cloud spend.

## Work

- Create a uv project: `uv init` (or write `pyproject.toml`) with deps: `boto3`, `strands-agents`, `bedrock-agentcore`, `bedrock-agentcore-starter-toolkit`, `ruff`, `pytest`. Run `uv sync`. (If a package name differs in the index, re-query AWS Knowledge MCP / PyPI and record the actual names used.)
- Install the AgentCore CLI: `npm install -g @aws/agentcore` (Node v26 present). Capture `agentcore --version`. If install fails, record `agentcore_cli: "unavailable"` in config.json and note the boto3 control-plane fallback for later phases.
- Create repo skeleton: `agent/`, `scripts/`, `docs/`, `dataset/`, `results/`, `tests/`, plus `.gitignore` (ignore `.venv/`, `__pycache__/`, `*.pyc`, any local creds) and a `README.md` stub.
- Write `preflight.py` that prints a capability + prerequisite report and writes `config.json`:
  - AWS identity via STS (`aws sts get-caller-identity`) — confirm account 434444145045; resolve region (`us-west-2`).
  - Bedrock model access: list foundation models + inference profiles; pick the cheapest enabled Claude profile as `agent_model_id` (prefer `us.anthropic.claude-haiku-4-5`, else `global.anthropic.claude-sonnet-4-6`). If no Claude access → exit non-zero with exact remediation.
  - CloudWatch Transaction Search status; if disabled, enable it (or record the exact enable step) so phase-4 evals can discover sessions.
  - Probe boto3 for AgentCore ops: build clients `bedrock-agentcore-control` and `bedrock-agentcore`; inspect `client.meta.service_model.operation_names` for harness ops (CreateHarness/InvokeHarness), evaluation ops (batch evaluation / create-evaluation), and optimization/recommendation ops. Record each as `present`/`absent`.
  - Write `config.json` with: `region`, `account`, `agent_model_id`, `judge`, `deploy_path` (`agentcore-cli` | `boto3-control`), `transaction_search` (on/off), and capability flags (`harness_api`, `evaluation_api`, `optimization_api`, `agentcore_cli`).
- Run `uv run ruff check .` and fix any lint.

## Acceptance criteria (all must pass — verify each in transcript)

- `uv run python -c "import boto3, strands, bedrock_agentcore"` exits 0.
- `agentcore --version` prints a version, OR config.json records `agentcore_cli: "unavailable"` with the documented fallback.
- `uv run python preflight.py` prints a report containing the identity (account 434444145045), region `us-west-2`, a Bedrock-model-access line, a Transaction-Search line, and a boto3 capability line for harness/eval/optimization (each present/absent).
- `preflight.py` writes `config.json` containing region, agent_model_id, judge, deploy_path, transaction_search, and the capability flags.
- If Bedrock Claude access is absent, `preflight.py` exits non-zero and prints exact remediation (this is an honest BLOCK).
- `uv run ruff check .` exits 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv sync`
- `uv run python preflight.py`
- `uv run ruff check .`

## Evidence required in transcript

- Last ~15 lines of the preflight report (identity, region, model access, transaction search, capability flags).
- `config.json` contents.
- `agentcore --version` output (or recorded unavailable).

## Notes

Treat ResourceNotFound / UnknownOperation when probing as "absent"; treat AccessDenied on a prerequisite as a blocker to surface. Do NOT hard-fail on absent managed-Harness ops — record the flag; phase 3 degrades gracefully. Keep `preflight.py` idempotent (safe to re-run).
