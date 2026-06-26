SUPERGOAL_PHASE_START
Phase: 2 of 6 — Build support agent + local smoke
Task: Implement the Strands customer-support agent with a deliberately weak baseline prompt and exercise it locally against real Bedrock.
Type: greenfield, real-cloud, ai-agents
Mandatory commands: uv run python -m py_compile agent/main.py agent/prompts.py scripts/smoke_local.py, uv run pytest -q, uv run python scripts/smoke_local.py, uv run ruff check .
Acceptance criteria: 5
Evidence required: pytest summary, smoke transcript for >=2 prompts, BASELINE_PROMPT text
Depends on phases: 1

## Why

Get the agent logic right and exercised against real Bedrock before any cloud deploy, and seed a weak baseline prompt so optimization later has measurable signal.

## Work

- `agent/prompts.py`: define `BASELINE_PROMPT` — a deliberately terse/weak system prompt (e.g. "You are a support bot. Answer questions.") with a comment marking it as the intentionally-weak baseline for the optimization phase. Reserve `OPTIMIZED_PROMPT = None` (filled in phase 5).
- `agent/main.py`: the Acme Store customer-support Strands agent following the documented pattern — ~5 `@tool` functions: `lookup_order(order_id)`, `initiate_return(order_id, reason)`, `check_shipping_status(order_id)`, `apply_discount(order_id, percent, reason)`, `escalate_to_human(reason)`. Build `Agent(model=BedrockModel(model_id=<from config.json>), tools=[...], system_prompt=BASELINE_PROMPT)`. Wrap with `BedrockAgentCoreApp` + `@app.entrypoint def invoke(payload, context): return {"response": str(agent(payload.get("prompt","Hello")))}` and `app.run()` under `__main__`. Set max output tokens explicitly on the model.
- `scripts/smoke_local.py`: import the agent (not the server) and call it directly on 3–4 prompts (order lookup, return, shipping delay), printing each answer and which tools fired.
- `tests/test_tools.py`: unit-test the deterministic tool functions (known order id returns the seeded record; unknown id returns the not-found shape).
- Read `agent_model_id` from `config.json`; do not hardcode the model.

## Acceptance criteria (all must pass — verify each in transcript)

- `uv run python -m py_compile agent/main.py agent/prompts.py scripts/smoke_local.py` exits 0.
- `uv run pytest -q` passes (tool functions verified for known + unknown order ids).
- `uv run python scripts/smoke_local.py` returns a non-empty answer to "What's the status of order ORD-1001?" that includes the order's status, AND the printed transcript shows a tool invocation (e.g. lookup_order) — both surfaced in the output.
- `BASELINE_PROMPT` is intentionally minimal and documented as such in a comment.
- `uv run ruff check .` exits 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv run python -m py_compile agent/main.py agent/prompts.py scripts/smoke_local.py`
- `uv run pytest -q`
- `uv run python scripts/smoke_local.py`
- `uv run ruff check .`

## Evidence required in transcript

- pytest summary line.
- smoke-test transcript for >=2 prompts showing the agent answer + tool call(s).
- The `BASELINE_PROMPT` text.

## Notes

If the installed `bedrock-agentcore` / `strands` import surface differs from the documented example, adapt to the actual API (re-query AWS Knowledge MCP) and record the shapes used in a code comment. Keep tool bodies deterministic so eval ground-truth is stable. Bound Bedrock spend: short prompts, low max_tokens.
