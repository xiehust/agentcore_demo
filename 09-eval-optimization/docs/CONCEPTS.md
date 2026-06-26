# Concepts: AgentCore Harness, Evaluations, Optimization

This demo exercises three Amazon Bedrock AgentCore capabilities that together form a
**closed quality loop**: observe → evaluate → improve. The agent is **created and deployed as a
managed AgentCore Harness**; the evaluate→optimize loop runs on a Runtime mirror of the same
agent (see the limitation note in §2). This doc explains each capability, maps it to the real
API, and points at the script that exercises it.

![closed loop](agentcore-closed-loop.svg)

---

## 1. The agent harness — the primary create + deploy path

A managed **AgentCore Harness** is a config-driven agent: you declare the model, system prompt,
and tools, and AgentCore runs the orchestration loop — **no container, no orchestration code**.
Two API calls do it: `CreateHarness` (control plane) + `InvokeHarness` (data plane).

- **How the demo uses it:** `scripts/harness_create.py` calls `create_harness` / `update_harness`
  with the model, a (deliberately weak) baseline system prompt, and **5 inline-function tools**
  (`lookup_order`, `initiate_return`, `check_shipping_status`, `apply_discount`, `escalate_to_human`).
  `scripts/harness_agent.py` runs the **client-side tool loop**: `InvokeHarness` streams a
  `toolUse`, the client executes it (`agent/harness_tools.dispatch` → `agent/orders.py`) and
  returns a `toolResult`, looping until the model finishes. `scripts/invoke_deployed.py` (default
  `--target harness`) is the live demo.
- **Tool schema gotcha:** the tool `type` is the snake_case enum `inline_function`, but the config
  key is camelCase `inlineFunction`. `inputSchema` is a free-form JSON schema.
- **Memory:** disabled on this single-turn demo (`memory={"disabled": {}}`) — otherwise `InvokeHarness`
  needs `bedrock-agentcore:ListEvents` on the harness memory resource.
- **Observability:** set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` via the harness
  `environmentVariables` so its telemetry includes message content (off by default).

There is also an alternative **AgentCore Runtime** path (bring-your-own Strands code, deployed with
the `agentcore` CLI) in `agent/main.py` — see §2 for why the demo runs evaluation there.

| | Managed Harness (primary) | AgentCore Runtime (eval mirror) |
|---|---|---|
| You provide | **configuration** (model/prompt/tools) | agent **code** (Strands) |
| Orchestration loop | provided (managed) | yours |
| API / deploy | `CreateHarness` / `InvokeHarness` | `agentcore deploy` / `InvokeAgentRuntime` |
| Tool execution | client-side tool loop (`toolUse`/`toolResult`) | server-side in the container |

---

## 2. AgentCore Evaluations

A managed service that scores agent behavior with **LLM-as-a-judge** (built-in + custom evaluators).
It reads the agent's **GenAI trajectory** — spans from `aws/spans` plus **events** (with
`body.input/output.messages` content) from the agent's runtime log group — via CloudWatch
Transaction Search, and converts them to a unified format for scoring.

- **Built-in evaluators used:** `Builtin.GoalSuccessRate` (did the agent accomplish the task),
  `Builtin.Helpfulness`, `Builtin.Faithfulness`.
- **Mode:** batch — a set of sessions as one job (`StartBatchEvaluation` → poll `GetBatchEvaluation`).
- **How the demo uses it:** `scripts/generate_sessions.py` invokes the agent over a 10-prompt dataset;
  `scripts/run_evaluation.py` scopes the job by `serviceNames`, the runtime log group, and
  `filterConfig.sessionIds`, polls to terminal, and writes `results/<tag>_scores.json`.

> ### ⚠️ Why evaluation runs on the Runtime mirror, not the Harness
> AgentCore Evaluations supports the `strands.telemetry.tracer` scope, but the **managed harness's**
> telemetry emits message content in a **double-nested `content.content` (stringified)** shape that
> the evaluator's agent-span mapper cannot parse into `body.input.messages[].content`. Every harness
> session therefore fails with **`AgentSpanMappingException: Failed to parse user_query`**, even with
> content capture enabled. The eval-mappable path is a **Strands agent on AgentCore Runtime with
> `aws-opentelemetry-distro` (ADOT)**, which emits the standard event shape. So the demo:
> - **deploys + serves the agent as the managed Harness** (the requested create/deploy path), and
> - runs **evaluation + optimization against the Runtime mirror of the same agent** (same 5 tools,
>   same prompts), applying any improvement to **both**.
>
> Diagnosed precisely from the eval results stream; see `agentcore-api-surface` memory for evidence.

---

## 3. AgentCore Optimization

Turns evaluation findings into validated improvements — the **improve** step.

### Recommendations (executed)
`scripts/run_optimization.py` calls `StartRecommendation` (`type=SYSTEM_PROMPT_RECOMMENDATION`) against
the baseline batch evaluation, targeting `Builtin.GoalSuccessRate`. It analyzes the traces, returns
an optimized system prompt + rationale (`results/recommendation.json`), writes it to `agent/prompts.py`
as `OPTIMIZED_PROMPT`, **and applies it to the harness via `UpdateHarness`**.

### Validate before promote — the loop's whole point
The improved prompt is re-deployed to the Runtime mirror, a fresh session set is generated, and
`run_evaluation.py` runs again → `results/improved_scores.json`. `scripts/compare.py` writes
`results/comparison.json` with per-evaluator deltas and a **promote / do-not-promote** decision.

**What happened in this run (an instructive, honest outcome):** the baseline scored `GoalSuccessRate
= 1.0` (no failures), so the recommendation had no failure traces to learn from and added a generic
"wait for explicit approval before acting" safety invariant. That made the agent *ask* instead of
*completing* tasks, regressing `GoalSuccessRate` **1.0 → 0.6**. The evaluation **caught the
regression**, the comparison returned **`promote: false`**, and the change was **rolled back to the
baseline prompt**. This is the protective value of evaluate-before-promote: the loop prevented a
quality regression from shipping. (A weaker baseline with real failures typically yields a promotable
gain instead.)

### A/B Testing (documented runbook)
`CreateABTest` splits **live** traffic between control (baseline) and treatment (optimized) variants
via an AgentCore Gateway, scores each session with an online evaluator, and reports statistical
significance. It needs a Gateway + config bundles + an online evaluation config + live traffic, so
`scripts/ab_test.py` provides the exact setup as a runbook rather than standing up that infrastructure.

---

## The loop, end to end

```
Create + deploy agent  ── managed AgentCore Harness (model + weak prompt + 5 inline tools)
  │                        invoke via InvokeHarness client-side tool loop
  │  (same agent mirrored on AgentCore Runtime for eval-mappable traces)
  ▼
Observe   GenAI spans + content events → CloudWatch Transaction Search
  ▼
Evaluate  AgentCore Evaluations (batch, LLM-judge) → baseline scores
  ▼
Optimize  Recommendations → improved prompt → apply to harness (UpdateHarness) + redeploy mirror
  ▼
Validate  re-evaluate → compare → PROMOTE or (this run) DO NOT PROMOTE → roll back
```
