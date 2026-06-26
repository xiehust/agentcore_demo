# THINKING — AgentCore harness / evaluation / optimization closed-loop demo

## Goal

Build a **simplified, hands-on, end-to-end closed-loop demo** on **real Amazon Bedrock AgentCore** (account 434444145045, region us-west-2) that:
1. Creates an **agent harness** (a customer-support FAQ agent) and **deploys it to AgentCore**.
2. **Evaluates** it with AgentCore Evaluations (LLM-as-a-Judge, batch mode, built-in evaluators).
3. **Optimizes** it with AgentCore Optimization (Recommendations → improved system prompt → re-evaluate → compare), closing the **observe → evaluate → improve** loop.
4. Teaches the three concepts thoroughly (docs + architecture diagram + API mapping).

User chose: **Full real AgentCore** · **Customer-support FAQ agent** · **Thorough**.

## What I learned (research via AWS Knowledge MCP + web + claude-api skill)

AgentCore is GA (Oct 2025), a platform to *build, connect, optimize* agents. Relevant pieces:

- **AgentCore Runtime** — serverless agent hosting. Containers expose `/ping` + `/invocations` (port 8080, ARM64) OR use direct code deployment. Invoked via `InvokeAgentRuntime`. The `agentcore` CLI (`@aws/agentcore`, npm, Node 20+) scaffolds + deploys (handles IAM role, ECR/CodeBuild, CloudWatch). Exact recipe in the batch-evaluations getting-started.
- **AgentCore Harness** (GA Jun 2026) — *managed* agent harness, powered by Strands. `CreateHarness` (control plane `bedrock-agentcore-control`) + `InvokeHarness` (data plane `bedrock-agentcore`). Config-driven (model, tools, skills, instructions); no container, no orchestration code. `runtimeSessionId` must be ≥33 chars. Defaults to Claude Sonnet 4.6 on Bedrock. This is the literal "agent harness" the user wants to understand.
- **AgentCore Evaluations** (GA Mar 2026) — LLM-as-a-Judge scoring. Built-in evaluators incl. `Builtin.GoalSuccessRate`, `Builtin.Helpfulness`, `Builtin.Faithfulness`. Modes: online / on-demand / **batch**. Reads agent sessions from CloudWatch Logs (needs **Transaction Search enabled**). CLI: `agentcore run batch-evaluation --evaluator ... --wait`; also boto3 / SDK.
- **AgentCore Optimization** (GA Jun 2026) — the improve loop: **Recommendations** (analyze traces+eval failures → optimized system prompt / tool descriptions), **config bundles** (immutable versioned config, swap without redeploy), **A/B testing** (Gateway traffic split + online eval w/ statistical significance), then promote winner. Works regardless of where the agent runs.

Bedrock model IDs on Strands `BedrockModel`: `global.anthropic.claude-sonnet-4-6` (canonical example) or `us.anthropic.claude-...` inference profiles. Will verify access + pick a cheap model (Haiku 4.5 inference profile) in preflight. Use `aws bedrock list-foundation-models` / `list-inference-profiles`.

## Architecture decision (most-robust path for an unsupervised run)

The eval+optimization getting-started is built around a **Strands agent on AgentCore Runtime** with observability — that is the exact, complete, documented recipe (the "Acme Store" customer-support agent with 5 tools). So:

- **Core closed loop = Strands customer-support agent on AgentCore Runtime** (deploy via `agentcore` CLI) + **AgentCore Evaluations** (batch, built-in evaluators) + **Optimization** (recommendations → improved prompt → re-eval → compare). This maximizes success probability because every step has an exact recipe and produces CloudWatch traces the eval/recommendations APIs consume. The Runtime-hosted agent *is* "an agent harness deployed to AgentCore."
- **Harness understanding = CONCEPTS.md (thorough) + a `harness_demo.py`** using real `CreateHarness`/`InvokeHarness`, attempted in phase 3, **degrading gracefully** to a documented runnable script if the installed boto3 lacks the brand-new harness ops. This satisfies "了解 AgentCore harness" + "create an agent harness" without making the newest/riskiest API a hard blocker for the closed loop.

Seed a **deliberately weak baseline system prompt** so optimization has measurable signal (baseline scores < improved scores).

## Constraints

- Real AWS resources + Bedrock + eval jobs cost money (a few dollars). Outward-facing. The Supergoal plan-review gate is the consent point. **A teardown script is mandatory** (phase 6) to stop ongoing cost.
- Bedrock Claude model access must be enabled in the account → preflight verifies, fails fast with clear instructions if not (a legitimate BLOCK).
- Evaluations need CloudWatch **Transaction Search** enabled → preflight enables/verifies.
- Several steps have real-world waits (CloudWatch ingestion 2–3 min; batch eval jobs minutes). Scripts must poll terminal states, not assume instant.
- No git in cwd yet → `git init` before dispatch so deliverable/cleanliness checks work.

## Risks (top 3) + dependencies

1. **Brand-new AgentCore APIs may not be in installed boto3 / agentcore CLI** (harness, evaluations, recommendations all 2026 GA). → Preflight (phase 1) probes boto3 service-model ops + CLI subcommands and prints a capability report; later phases prefer the CLI recipe (most documented) and degrade the riskiest API (managed Harness) to a script+doc. Fail fast, no thrashing.
2. **Bedrock model access / IAM / Transaction Search not enabled** → blocks deploy/eval. Preflight verifies all three up front and emits exact remediation. A genuine unmet prerequisite is an honest BLOCK surfaced to the user, not an infinite retry.
3. **Long external waits + cost in an unsupervised loop** → scripts poll with bounded timeouts and print progress; teardown script provided; cost notes in README; the closed loop is designed to need only a small session set (≈10 prompts) per eval round.

**Dependency order:** preflight → agent code (local smoke) → deploy (Runtime + harness demo) → generate sessions + baseline eval → recommendations + re-eval + compare → docs/diagram/teardown/polish. Each phase depends on all prior.

## Memory hits applied

None — memory dir empty (clean run). Will write a `project_*` memory at the final phase recording location/stack/status + the AgentCore API surface learned, so future runs start smarter.

## Tools / skills relied on

- AWS Knowledge MCP (`mcp__aws-knowledge__*`) — primary research; can re-query exact API shapes mid-run.
- `mcp__web-search__search` / WebFetch — fallback research.
- `fireworks-tech-graph` skill — render the closed-loop architecture diagram in phase 6.
- AWS `amazon-bedrock` agent skill (retrievable via the MCP) — accurate Bedrock/AgentCore API guidance if needed mid-run.
- `aws` CLI 2.34.53, `uv` 0.11, Python 3.14, Node v26 (`npm`), `gh` authed — all present.

## Best practices applied

- Set `maxTokens` explicitly on every Bedrock call (avoid silent quota over-reservation / ThrottlingException).
- Use cross-region inference profile IDs (`us.`/`global.` prefix) for Claude on Bedrock.
- Pluggable, idempotent scripts with bounded polling; capture all ARNs/IDs to JSON so phases compose.
- Least-privilege note + secrets hygiene (no creds in repo; `.gitignore`); teardown to bound cost.
- Honest evidence: every phase surfaces real command output + exit codes; partial success is reported as such, not papered over.
