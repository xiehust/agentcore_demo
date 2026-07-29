# Tool-using managed-Harness sessions cannot be scored by AgentCore Evaluations (`AgentSpanMappingException: Failed to parse user_query`)

**Service:** Amazon Bedrock AgentCore — managed **Harness** + **Evaluations** (batch)
**Region:** `us-west-2`
**Date verified:** 2026-06-27
**Severity:** High — any agent deployed as a managed Harness that uses inline-function tools cannot participate in the observe → evaluate → optimize loop.

---

## Summary

A managed AgentCore Harness emits full GenAI telemetry, and harness sessions that make **no tool call evaluate fine**. But harness sessions that **use an inline-function tool fail every evaluator** with:

```
error.type:    AgentSpanMappingException
error.message: Failed to parse user_query from agent-span with spanId: <id> and scope: strands.telemetry.tracer
```

**Root cause:** harness inline-function tools run via a **client-side tool loop** — each tool call requires a *second* `InvokeHarness` request (to return the `toolResult`). AgentCore records **each `InvokeHarness` request as a separate trace** (its own root `POST /invocations` span) that shares the `session.id` but is **not joined into one trace tree**. A tool-using session therefore spans **≥2 disjoint traces**, and the Evaluations span-mapper — which expects one connected agent trace per session — cannot assemble the trajectory / extract `user_query`.

> **This corrects an earlier hypothesis.** The telemetry's double-nested `content.content` shape is **not** the cause. That stringified shape appears in **both** harness and Runtime telemetry, and a Runtime session carrying the *identical* shape scores fine — so the mapper tolerates it. The real differentiator is multi-trace fragmentation from the client-side tool loop.

## Environment

| Field | Value |
|---|---|
| Harness model | `global.anthropic.claude-haiku-4-5-20251001-v1:0` (`converse_stream`) |
| Tools | 5 `inline_function` tools (client-side execution) |
| Content capture | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, `AGENT_OBSERVABILITY_ENABLED=true` |
| Telemetry scope | `strands.telemetry.tracer`; `telemetry.auto.version=0.18.0-aws`, `telemetry.sdk.version=1.42.1` |
| Evaluators | `Builtin.GoalSuccessRate`, `Builtin.Helpfulness`, `Builtin.Faithfulness` |
| CloudWatch Transaction Search | enabled (ACTIVE) |

## Expected behavior

A harness session (with content capture enabled) should be scoreable by AgentCore Evaluations regardless of whether it used tools — the same as a Strands-on-Runtime agent that runs the tool loop server-side.

## Actual behavior — the discriminating experiment

Five sessions on the **same** harness, identical except for tool use, then one `StartBatchEvaluation`:

| Session | Prompt | Tools used | Trace structure | Result |
|---|---|---|---|---|
| greeting | "what can you help with?" | none | 1 trace, 1 root span | ✅ scored 3/3 evaluators |
| thanks | "thanks, that's all" | none | 1 trace, 1 root span | ✅ scored 3/3 |
| policy | "how do returns work generally?" | none | 1 trace, 1 root span | ✅ scored 3/3 |
| lookup | "status of ORD-1001?" | `lookup_order` | **2 traces, 2 root spans** | ❌ `AgentSpanMappingException` (×3) |
| discount | "ORD-1003 is late, I want a discount" | `check_shipping_status`, `lookup_order` | **2 traces, 2 root spans** | ❌ `AgentSpanMappingException` (×3) |

Per-session errors are written to `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default`:

```json
{ "session.id": "<tool session>",
  "error.type": "AgentSpanMappingException",
  "error.message": "Failed to parse user_query from agent-span with spanId: 984fbeffc2ee71d1 and scope: strands.telemetry.tracer" }
```

## Root cause — trace structure

A tool-using harness session is split across multiple `InvokeHarness` requests by the client-side tool loop, and each request is its own trace:

```
Turn 1  InvokeHarness(prompt)                 → trace A, root span "POST /invocations"
        model emits toolUse, stops
        client executes the tool locally
Turn 2  InvokeHarness(prompt + toolResult)    → trace B, root span "POST /invocations"
        model emits final answer
```

Observed span trees (same `session.id`):

```
NO-TOOL session  (PASSES):  1 traceId,  roots = [POST /invocations]
TOOL session     (FAILS):   2 traceIds, roots = [POST /invocations, POST /invocations]
```

For comparison, the equivalent agent on **AgentCore Runtime** runs the whole tool loop server-side inside one invocation:

```
RUNTIME session  (PASSES):  roots = [POST /invocations, AgentCore.Runtime.Invoke]   (single connected agent trace)
```

The Runtime agent also has an `AgentCore.Runtime.Invoke` wrapper span that the harness does not emit. Either way, the Runtime session is one connected trace per session, which the mapper handles; the harness tool-loop session is not.

> Confirmed independently by `agentcore export harness`: AWS's own codegen of this harness is plain standard Strands (`BedrockModel` + standard `Agent`) with no custom telemetry — i.e. the agent design is fine; the fragmentation comes from how the *managed harness runtime* records each `InvokeHarness` request as its own trace.

## Minimal reproduction

1. Create a managed Harness with ≥1 inline-function tool, content capture + observability on.
2. Invoke it once with a prompt that triggers a tool (≥2 `InvokeHarness` calls in the client-side loop) and once with a prompt that triggers no tool.
3. Wait for Transaction Search ingestion (~5–10 min).
4. `StartBatchEvaluation` scoped to `harness_<Name>.DEFAULT`, the harness log group, and the session IDs.
5. Poll → the no-tool session scores; every tool-using session fails with `AgentSpanMappingException: Failed to parse user_query`.

> Reproduced end-to-end in this repo: invoke the harness with no-tool vs tool prompts, then `scripts/run_evaluation.py`.

## Impact

- Any **tool-driven** agent deployed as a managed Harness cannot be evaluated/optimized via AgentCore Evaluations — and tool use is the whole point of most agents.
- Teams must deploy a **second, redundant** Strands-on-Runtime copy of the same agent (which runs the tool loop server-side → one trace) solely to obtain scoreable telemetry, and apply every change to both. This is the workaround the `09-eval-optimization` demo uses.

## Suggested fixes (any one resolves it)

1. **Evaluations side (preferred):** group a session's spans by `session.id` across traces and stitch the multi-trace, multi-root structure into one logical agent trajectory before extracting `user_query`. The client-side tool loop inherently produces one trace per `InvokeHarness` request.
2. **Harness side:** propagate trace context across the client-side tool-loop `InvokeHarness` requests so a single tool-using session forms one connected trace (single root), as the Runtime path does.
3. **Documentation:** state that tool-using managed-Harness sessions are not yet scoreable by Evaluations, and recommend the Runtime path for closed-loop use with tool-driven agents.

## Workaround (current)

Run evaluation/optimization against a **Strands-on-Runtime mirror** of the same agent (`agentcore deploy`), which runs the tool loop server-side → one connected trace per session → eval-mappable, and apply any improvement to both Harness and Runtime. See `docs/CONCEPTS.md` §2.

## Open / untested

Only *client-side* `inline_function` tools fragment the trace. **Server-side** harness tools (MCP/Gateway/code-interpreter/browser) execute within a single `InvokeHarness` invocation and would likely keep one trace → may be evaluable. Not yet tested.

## References

- `docs/CONCEPTS.md` §2 — "Why evaluation runs on the Runtime mirror, not the Harness"
- Per-session error log group: `/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default`
- Re-verify: invoke the harness with a no-tool prompt vs a tool prompt, then `scripts/run_evaluation.py`
