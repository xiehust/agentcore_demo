SUPERGOAL_PHASE_START
Phase: 3 of 6 — Deploy to AgentCore
Task: Deploy the agent to real AgentCore Runtime and demonstrate the managed AgentCore Harness; capture all resource identifiers.
Type: real-cloud, ai-agents
Mandatory commands: uv run python scripts/invoke_deployed.py "What's the status of order ORD-1001?", uv run python scripts/harness_demo.py, uv run ruff check .
Acceptance criteria: 5
Evidence required: deploy success + runtime ARN, invoke_deployed transcript, harness_demo output, deployment.json
Depends on phases: 1, 2

## Why

Put the agent harness on real AgentCore so it emits observability traces that Evaluations + Optimization consume, and demonstrate the managed Harness feature the user wants to understand.

## Work

- Deploy `agent/main.py` to AgentCore **Runtime**. Preferred: the `agentcore` CLI (`agentcore configure` then `agentcore launch`/`deploy`) — it provisions the IAM execution role, packaging (CodeBuild/direct-code, no local Docker required), endpoint, and observability. If the CLI is `unavailable` per config.json, deploy via boto3 `bedrock-agentcore-control` `CreateAgentRuntime` with direct code deployment (re-query AWS Knowledge MCP for the exact op + payload).
- Capture the runtime ARN, endpoint/service name, and CloudWatch log group to `deployment.json` (read from `agentcore status --json` or the CreateAgentRuntime response).
- `scripts/invoke_deployed.py <prompt>`: invoke the deployed Runtime agent via `bedrock-agentcore` data-plane `invoke_agent_runtime` (or `agentcore invoke`) using a >=33-char session id; print the response.
- `scripts/harness_demo.py`: demonstrate the **managed Harness**. If `harness_api` is `present` in config.json: `create_harness` (name + execution role + model + customer-support instructions/tools as supported by the actual API shape — introspect the input shape), poll `get_harness` until READY, then `invoke_harness` with a >=33-char `runtimeSessionId` and stream the response; capture the harness ARN to `deployment.json`. If `harness_api` is `absent`: print a clear degraded message that explains the managed Harness, names the exact `CreateHarness`/`GetHarness`/`InvokeHarness` call sequence, states why it degraded (SDK lacks the op), and exits 0.
- Ensure observability/OTEL tracing is enabled on the deploy so phase-4 evaluation can discover sessions.
- Run `uv run ruff check .`.

## Acceptance criteria (all must pass — verify each in transcript)

- The deploy reports success and `deployment.json` contains a non-empty `agent_runtime_arn` and `log_group`; `agentcore status` (or the control-plane describe) shows READY/deployed.
- `uv run python scripts/invoke_deployed.py "What's the status of order ORD-1001?"` returns a response from the deployed agent (transcript shown).
- `uv run python scripts/harness_demo.py` exits 0 — either a live InvokeHarness streamed response (harness ARN captured) or the degraded doc-mode message naming the CreateHarness/InvokeHarness sequence.
- `deployment.json` is valid JSON with runtime ARN, region, and (if created) harness ARN.
- `uv run ruff check .` exits 0.

## Mandatory commands (run each, surface last ~10 lines + exit code)

- `uv run python scripts/invoke_deployed.py "What's the status of order ORD-1001?"`
- `uv run python scripts/harness_demo.py`
- `uv run ruff check .`

## Evidence required in transcript

- Deploy command output tail showing success + the captured runtime ARN.
- `invoke_deployed.py` transcript (prompt -> agent response).
- `harness_demo.py` output (live invoke or degraded doc-mode message).
- `deployment.json` contents.

## Notes

Deploys can take minutes — poll, don't assume instant. If CodeBuild/packaging fails, capture the exact error and fall back to direct-code deployment. Keep the same logical agent/model as phase 2 so eval ground-truth carries over. Record which deploy path actually succeeded in `deployment.json`.
