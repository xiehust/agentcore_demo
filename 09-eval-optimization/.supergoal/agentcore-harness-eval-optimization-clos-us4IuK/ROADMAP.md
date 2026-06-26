# Roadmap: AgentCore harness → evaluation → optimization closed-loop demo

**Task:** Build a simplified hands-on demo that creates a customer-support agent harness, deploys it to real Amazon Bedrock AgentCore (us-west-2), then runs AgentCore Evaluations and Optimization to close the observe→evaluate→improve loop.
**Type:** greenfield, real-cloud, ai-agents
**Created:** 2026-06-26
**Total phases:** 6

## Context summary

- **Stack:** Python 3.14 (uv-managed venv) + Strands Agents + `bedrock-agentcore` SDK + boto3; `agentcore` CLI (`@aws/agentcore`, Node 20+) for deploy/eval; AWS account 434444145045, region **us-west-2**.
- **Package manager:** `uv` for Python, `npm -g` for the AgentCore CLI.
- **Build / test / lint commands:** `uv run python -m py_compile <files>` (compile/syntax), `uv run python preflight.py` / script smoke runs, `uv run ruff check .` (lint), `uv run pytest -q` (unit tests for local logic).
- **Risky areas:** brand-new 2026-GA AgentCore APIs (harness/eval/optimization) may lag installed SDK/CLI; Bedrock model access + CloudWatch Transaction Search must be enabled; real cost + long external waits in an unsupervised run.

## Assumptions

Non-blocking decisions recorded so we can proceed without round-trips. If any are wrong, stop and tell us:

- Region **us-west-2** (the account's configured default + a primary AgentCore GA region) is used throughout.
- Agent model is a **cheap Claude inference profile on Bedrock** (prefer Haiku 4.5, e.g. `us.anthropic.claude-haiku-4-5`; fall back to `global.anthropic.claude-sonnet-4-6` if Haiku isn't enabled) to keep cost low; the LLM-judge uses a built-in AgentCore evaluator (its own judge model).
- The **core closed loop** runs on a **Strands customer-support agent deployed to AgentCore Runtime** (the documented eval/optimization vehicle); the **managed AgentCore Harness** (`CreateHarness`/`InvokeHarness`) is demonstrated separately and degrades to a runnable script + doc if the installed boto3 lacks those ops.
- Deployment uses the `agentcore` CLI with **CodeBuild/direct-code** packaging (no dependency on local Docker); preflight confirms the available path.
- "A/B testing" is delivered as a **runnable script + runbook** (real Gateway traffic-split needs sustained live traffic, which is synthetic in a demo); the *executed* closed loop is recommendations → improved prompt → re-eval → before/after comparison.
- The eval session set is small (~10 prompts/round) to bound cost and time.
- Repo is initialized with git at dispatch so deliverable + cleanliness checks work.

## Risk top 3

1. **New AgentCore APIs lag installed SDK/CLI** — likelihood: medium, mitigation: phase-1 preflight probes boto3 ops + CLI subcommands and prints a capability report; later phases prefer the documented CLI recipe and degrade the riskiest managed-Harness API to a script+doc. Fail fast, no thrashing.
2. **Prerequisites not enabled (Bedrock model access / IAM / Transaction Search)** — likelihood: medium, mitigation: preflight verifies all three and emits exact remediation; an unmet prerequisite is surfaced as an honest BLOCK with instructions, not retried forever.
3. **Real cost + long external waits unsupervised** — likelihood: high (waits), mitigation: bounded polling with progress prints; tiny session set; mandatory teardown script + README cost notes; plan-review gate is the user's consent point.

## Phase map

| # | Phase | Depends on | Deliverable |
|---|-------|------------|-------------|
| 1 | Preflight & scaffold | — | uv project + AgentCore CLI installed; `preflight.py` capability/prereq report (PASS or clear blockers) |
| 2 | Build support agent + local smoke | 1 | Strands customer-support agent (weak baseline prompt) running locally against real Bedrock |
| 3 | Deploy to AgentCore | 1, 2 | Agent live on AgentCore Runtime (`agentcore deploy/invoke`); managed-Harness demo (CreateHarness/InvokeHarness or degraded script); ARNs captured |
| 4 | Generate sessions + baseline evaluation | 1, 2, 3 | Eval dataset; sessions generated; AgentCore batch evaluation run; **baseline scores** captured |
| 5 | Optimization closed loop | 1, 2, 3, 4 | Recommendations → improved prompt → re-eval; **before/after comparison** proving the loop closed |
| 6 | Docs, diagram, teardown & Polish/Harden | 1..5 | README runbook + CONCEPTS.md + architecture diagram + teardown script; every aspect verified |

---

## Phase 1 — Preflight & scaffold

**Why:** Fail fast on missing prerequisites/SDK gaps and lock the toolchain before spending money or time on the cloud.

**Deliverables:**
- `pyproject.toml` (uv project: boto3, strands-agents, bedrock-agentcore, bedrock-agentcore-starter-toolkit, ruff, pytest) + locked venv
- `preflight.py` — verifies AWS identity/region, Bedrock Claude model access (`list-foundation-models`/`list-inference-profiles`), CloudWatch Transaction Search status, AgentCore CLI version, and probes boto3 for harness/evaluation/optimization ops; prints a capability report and chosen model/deploy path to `config.json`
- `README.md` stub, `.gitignore`, repo skeleton (`agent/`, `scripts/`, `docs/`)

**Acceptance criteria:**
- [ ] `uv run python -c "import boto3, strands, bedrock_agentcore"` exits 0 (deps installed)
- [ ] `agentcore --version` prints a version (CLI installed) OR `config.json` records `agentcore_cli: "unavailable"` with the documented fallback path
- [ ] `uv run python preflight.py` runs and prints a report containing `AWS identity: 434444145045`, the resolved region `us-west-2`, a Bedrock-model-access line, a Transaction-Search line, and a boto3 capability line for harness/eval/optimization (each `present`/`absent`)
- [ ] `preflight.py` writes `config.json` with keys: `region`, `agent_model_id`, `judge`, `deploy_path`, `transaction_search`, and capability flags
- [ ] If Bedrock Claude access is **absent**, `preflight.py` exits non-zero and prints exact remediation (enable model access in console) — surfaced as a BLOCK, not retried
- [ ] `uv run ruff check .` exits 0

**Mandatory commands:**
- `uv sync`
- `uv run python preflight.py`
- `uv run ruff check .`

**Evidence required:**
- Last ~15 lines of the `preflight.py` report including identity, region, model-access, Transaction-Search, and capability lines
- `config.json` contents
- `agentcore --version` output (or recorded unavailable)

**Dependencies:** none

**Notes:** Enable Transaction Search if absent (`aws logs ...` / SDK) so phase-4 evals can discover sessions. Pick the cheapest enabled Claude inference profile as `agent_model_id`. Do not hard-fail on missing managed-Harness ops — record the flag; phase 3 degrades. Treat ResourceNotFound/UnknownOperation as "absent", AccessDenied as a prerequisite blocker.

---

## Phase 2 — Build support agent + local smoke

**Why:** Get the agent logic correct and exercised against real Bedrock before any cloud deploy, and seed a weak baseline prompt so optimization has measurable signal.

**Deliverables:**
- `agent/main.py` — Strands customer-support agent (the Acme Store pattern: ~5 tools — `lookup_order`, `initiate_return`, `check_shipping_status`, `apply_discount`, `escalate_to_human`) wrapped with `BedrockAgentCoreApp` `@app.entrypoint`, model from `config.json`
- `agent/prompts.py` — `BASELINE_PROMPT` (deliberately terse/weak) and a place for the optimized prompt
- `scripts/smoke_local.py` — invokes the agent locally against real Bedrock on 3–4 prompts and prints responses + which tools fired
- `tests/test_tools.py` — unit tests for the deterministic tool functions

**Acceptance criteria:**
- [ ] `uv run python -m py_compile agent/main.py agent/prompts.py scripts/smoke_local.py` exits 0
- [ ] `uv run pytest -q` passes (tool functions return expected structured strings for known/unknown order IDs)
- [ ] `uv run python scripts/smoke_local.py` returns a non-empty answer to "What's the status of order ORD-1001?" that includes the order's status, and the printed transcript shows a tool invocation (e.g. `lookup_order`)
- [ ] `BASELINE_PROMPT` is intentionally minimal (documented in a comment as the weak baseline for optimization)
- [ ] `uv run ruff check .` exits 0

**Mandatory commands:**
- `uv run python -m py_compile agent/main.py agent/prompts.py scripts/smoke_local.py`
- `uv run pytest -q`
- `uv run python scripts/smoke_local.py`
- `uv run ruff check .`

**Evidence required:**
- pytest summary line
- smoke-test transcript for ≥2 prompts showing the agent answer + tool call(s)
- The `BASELINE_PROMPT` text

**Dependencies:** 1

**Notes:** Use Strands `BedrockModel(model_id=config.agent_model_id)`; set max tokens explicitly. If `bedrock_agentcore.runtime` import differs in the installed SDK, adapt to the actual entrypoint API (re-query AWS Knowledge MCP) and record the shape used.

---

## Phase 3 — Deploy to AgentCore

**Why:** Put the agent harness on real AgentCore so it emits observability traces that Evaluations and Optimization consume, and demonstrate the managed Harness feature.

**Deliverables:**
- Deployed AgentCore **Runtime** agent via `agentcore configure`/`agentcore launch`(or `deploy`) using `agent/main.py`; runtime ARN + service name + log group captured to `deployment.json`
- `scripts/harness_demo.py` — managed-Harness demo using `CreateHarness`/`GetHarness`(poll READY)/`InvokeHarness`; if those boto3 ops are `absent` per `config.json`, the script prints a clear "managed Harness API unavailable in this SDK — here is the exact call sequence" doc-mode message and exits 0 (degraded, not failed)
- `scripts/invoke_deployed.py` — invokes the deployed Runtime agent (`InvokeAgentRuntime`) and prints the response

**Acceptance criteria:**
- [ ] `agentcore status` (or `config.json` deploy path) shows the runtime in a READY/deployed state; `deployment.json` contains a non-empty `agent_runtime_arn` and `log_group`
- [ ] `uv run python scripts/invoke_deployed.py "What's the status of order ORD-1001?"` returns a response from the deployed agent (transcript shown)
- [ ] `uv run python scripts/harness_demo.py` exits 0 — either prints a live `InvokeHarness` streamed response (harnessArn captured to `deployment.json`) OR prints the degraded doc-mode message naming `CreateHarness`/`InvokeHarness` and why it degraded
- [ ] `deployment.json` is valid JSON with the runtime ARN, region, and (if created) harness ARN
- [ ] `uv run ruff check .` exits 0

**Mandatory commands:**
- `uv run python scripts/invoke_deployed.py "What's the status of order ORD-1001?"`
- `uv run python scripts/harness_demo.py`
- `uv run ruff check .`

**Evidence required:**
- Deploy command output tail showing success + the captured runtime ARN
- `invoke_deployed.py` transcript (prompt → agent response)
- `harness_demo.py` output (live invoke or degraded doc-mode message)
- `deployment.json` contents

**Dependencies:** 1, 2

**Notes:** Prefer the `agentcore` CLI for Runtime deploy (handles IAM/ECR/CodeBuild/observability). If the CLI is unavailable, deploy via boto3 control-plane `CreateAgentRuntime` with direct code deployment (re-query AWS Knowledge MCP for the exact op). Ensure observability/OTEL is on so traces reach CloudWatch. Reuse the same `runtimeSessionId` (≥33 chars) format documented for harness.

---

## Phase 4 — Generate sessions + baseline evaluation

**Why:** Produce real agent sessions and score them with AgentCore Evaluations to establish the baseline the optimization loop improves on.

**Deliverables:**
- `dataset/eval_prompts.json` — ~10 customer-support prompts (order lookups, returns, shipping delays, discount requests, multi-tool) with notes on expected behavior
- `scripts/generate_sessions.py` — invokes the deployed agent over the dataset (unique session IDs), then waits for CloudWatch ingestion
- `scripts/run_evaluation.py` — starts an AgentCore **batch evaluation** with built-in evaluators (`Builtin.GoalSuccessRate`, `Builtin.Helpfulness`, `Builtin.Faithfulness`), polls to terminal state, and writes aggregate + per-session scores to `results/baseline_scores.json`

**Acceptance criteria:**
- [ ] `uv run python scripts/generate_sessions.py` invokes the agent for every dataset prompt and prints one line per session (id + ok)
- [ ] `uv run python scripts/run_evaluation.py` starts a batch evaluation and polls until a terminal state is reached (prints the job id + final state)
- [ ] `results/baseline_scores.json` exists and contains aggregate scores for each requested evaluator (numeric)
- [ ] `run_evaluation.py` handles "no sessions found yet" by retrying ingestion wait before failing (bounded), and surfaces the terminal state explicitly
- [ ] `uv run ruff check .` exits 0

**Mandatory commands:**
- `uv run python scripts/generate_sessions.py`
- `uv run python scripts/run_evaluation.py`
- `uv run ruff check .`

**Evidence required:**
- `generate_sessions.py` output (session ids created)
- `run_evaluation.py` output: job id, final state, aggregate scores
- `results/baseline_scores.json` contents

**Dependencies:** 1, 2, 3

**Notes:** Resolve `LOG_GROUP`/service name from `deployment.json` (or `agentcore status --json`). If the batch-evaluation API op is absent in boto3, use `agentcore run batch-evaluation --wait` and parse its output; record which path was used. Allow 2–3 min CloudWatch ingestion before starting the job. Keep evaluator set small to bound cost.

---

## Phase 5 — Optimization closed loop

**Why:** Demonstrate the observe→evaluate→improve loop end to end: turn evaluation findings into an improved agent and prove the improvement with a re-evaluation.

**Deliverables:**
- `scripts/run_optimization.py` — calls the **Recommendations** API against the agent's traces + baseline eval (targeting the goal-success evaluator) to produce an optimized system prompt / tool descriptions; writes the recommendation + rationale to `results/recommendation.json`; applies the improved prompt to `agent/prompts.py` (`OPTIMIZED_PROMPT`)
- Re-deploy (or config-bundle swap) the improved prompt; `scripts/generate_sessions.py` re-run for a new session set; `scripts/run_evaluation.py` re-run → `results/improved_scores.json`
- `scripts/compare.py` — prints a baseline-vs-improved table and writes `results/comparison.json` + a one-paragraph verdict on whether the loop closed
- `scripts/ab_test.py` — runnable A/B-test scaffold (config-bundle or target-based variants via Gateway) with a runbook comment; attempts a real A/B setup if the API is present, else documents the exact calls (degraded, exits 0)

**Acceptance criteria:**
- [ ] `uv run python scripts/run_optimization.py` produces a recommendation (optimized prompt text) saved to `results/recommendation.json` and updates `OPTIMIZED_PROMPT` in `agent/prompts.py` (or, if the Recommendations API is absent, generates an improved prompt via a documented Bedrock-judge fallback and records that path)
- [ ] The improved agent is re-deployed/re-configured and re-evaluated; `results/improved_scores.json` exists with numeric aggregate scores for the same evaluators as baseline
- [ ] `uv run python scripts/compare.py` prints a baseline-vs-improved comparison and writes `results/comparison.json` with both score sets + a `loop_closed` boolean and verdict
- [ ] The comparison shows improved ≥ baseline on the primary evaluator, OR `comparison.json` contains an honest analysis explaining the result (no silent papering-over)
- [ ] `uv run python scripts/ab_test.py` exits 0 (real setup or documented degraded mode)
- [ ] `uv run ruff check .` exits 0

**Mandatory commands:**
- `uv run python scripts/run_optimization.py`
- `uv run python scripts/compare.py`
- `uv run python scripts/ab_test.py`
- `uv run ruff check .`

**Evidence required:**
- `run_optimization.py` output: the optimized prompt + rationale (or documented fallback path)
- `results/comparison.json` contents (baseline vs improved + verdict)
- `compare.py` printed table
- `ab_test.py` output (real or degraded)

**Dependencies:** 1, 2, 3, 4

**Notes:** Re-query AWS Knowledge MCP for the exact Recommendations + config-bundle op names if boto3 shapes are unclear. Prefer config-bundle swap over full redeploy if available (faster, the documented optimization path). Keep the new session set the same size as baseline for a fair comparison.

---

## Phase 6 — Docs, diagram, teardown & Polish/Harden

**Why:** Catch what earlier phases missed, make the demo reproducible + understandable, and bound ongoing cost. This is how "every aspect is perfect" gets enforced.

**Sub-passes (each must produce evidence):**

- [ ] **Docs/runbook** — `README.md`: prereqs, one-command-per-step reproduce path (preflight → build → deploy → eval → optimize → teardown), cost estimate, troubleshooting
- [ ] **Concepts** — `docs/CONCEPTS.md`: clear explanation of AgentCore **Harness**, **Evaluations**, **Optimization** with the real API/CLI mapping and how the demo exercises each
- [ ] **Diagram** — architecture diagram of the closed loop (build→deploy→observe→evaluate→optimize) as SVG+PNG via the `fireworks-tech-graph` skill, referenced from README
- [ ] **Teardown** — `scripts/teardown.py` (or `.sh`) that deletes the runtime, harness (if created), and eval/optimization resources created by the demo; dry-run by default, `--yes` to execute; documented in README
- [ ] **States/edges** — scripts handle missing `config.json`/`deployment.json`, absent sessions, and absent APIs with clear messages (verified by reading each script's guards)
- [ ] **Security** — no AWS creds/secrets committed; `.gitignore` covers `config.json` secrets/`.venv`/results if sensitive; least-privilege note in README
- [ ] **Diff review** — final diff scanned for stray debug prints / TODOs from this run
- [ ] **Regression sweep** — `uv run python -m py_compile` over all scripts, `uv run pytest -q`, `uv run ruff check .` all green

**Mandatory commands:**
- `uv run python -m py_compile agent/*.py scripts/*.py`
- `uv run pytest -q`
- `uv run ruff check .`

**Evidence required:**
- One paragraph per sub-pass with what was checked / found / fixed
- `ls` of `docs/` + diagram files (SVG + PNG) and a README excerpt showing the reproduce runbook
- Confirmation `teardown.py --help`/dry-run runs and lists target resources
- Final `git diff --stat` summary + final test/lint/compile summary

**Dependencies:** 1, 2, 3, 4, 5
