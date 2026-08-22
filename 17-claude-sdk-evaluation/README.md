# Claude Agent SDK → Langfuse → AgentCore Evaluations

This example runs a tool-using agent with **Claude Agent SDK** and
`claude-sonnet-5`, exports its OpenTelemetry trace to **Langfuse**, reads the
session and full trace back from the Langfuse API, and submits the reconstructed
session spans directly to the Amazon Bedrock AgentCore `Evaluate` API.

**CloudWatch is not used anywhere in this pipeline.** The AWS guide retrieves
spans from CloudWatch because that is its default telemetry store; the
on-demand `Evaluate` API itself accepts caller-provided `sessionSpans`.

## Architecture

![Claude Agent SDK to Langfuse to AgentCore architecture](assets/architecture.light.svg)

[Open the 1920px PNG](assets/architecture.light.png)

## Evaluation flow

![End-to-end evaluation flow](assets/evaluation-flow.light.svg)

[Open the 1920px PNG](assets/evaluation-flow.light.png)

The editable SVG files and their reproducible generator are under `assets/`.
Regenerate them with:

```bash
python3 assets/generate_diagrams.py
```

## Data flow

```text
Claude Agent SDK
  └─ OpenInference instrumentation
       └─ Langfuse OpenTelemetry exporter
            └─ Langfuse Sessions API + Trace API
                 └─ unified OpenInference session spans
                      └─ bedrock-agentcore.evaluate(...)
```

The conversion follows AgentCore's documented Claude Agent SDK contract:

- scope: `openinference.instrumentation.claude_agent_sdk`
- agent span: `openinference.span.kind=AGENT`
- tool span: `openinference.span.kind=TOOL`
- unified content: `input.value` and `output.value`
- correlation: `session.id`, `traceId`, and `spanId`

## Prerequisites

- Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/)
- A Claude-compatible endpoint that exposes `claude-sonnet-5`
- A Langfuse project
- AWS credentials with `bedrock-agentcore:Evaluate` permission
- Bedrock model access required by the selected built-in evaluator

The run invokes both Claude and an AgentCore evaluator and can incur charges.

## Environment

The program uses the existing process environment; it does not load or print
secret values.

```bash
export ANTHROPIC_BASE_URL='https://...'
export ANTHROPIC_API_KEY='...'

export LANGFUSE_PUBLIC_KEY='...'
export LANGFUSE_SECRET_KEY='...'
export LANGFUSE_BASE_URL='https://cloud.langfuse.com'
# LANGFUSE_HOST is also accepted for existing installations.

export AWS_REGION='us-west-2'
```

Standard AWS credential-chain configuration is supported (environment,
`~/.aws`, SSO, instance role, and so on). See `.env.example` for placeholders.

## Install and run

```bash
cd 17-claude-sdk-evaluation
uv sync
uv run claude-sdk-eval
```

The default prompt forces two deterministic MCP tool calls and the default
evaluator is `Builtin.Helpfulness`. Useful options:

```bash
# Run multiple evaluators (one Evaluate request per ID)
uv run claude-sdk-eval \
  --evaluator Builtin.Helpfulness \
  --evaluator Builtin.ToolSelectionAccuracy

# Override the prompt or region
uv run claude-sdk-eval \
  --prompt 'Use the tool to price one NOTEBOOK and two PEN items.' \
  --region us-west-2

# Validate Claude → Langfuse retrieval/conversion without calling AgentCore
uv run claude-sdk-eval --skip-evaluation
```

The model is fixed to the exact identifier `claude-sonnet-5`; there is no model
override so runs cannot silently evaluate a different model.

## Outputs

Local outputs are written under the gitignored `results/` directory:

- `results/session_spans.json`: spans read from Langfuse and converted to the
  AgentCore unified telemetry schema
- `results/evaluation.json`: IDs, agent response, span summary, and evaluator
  results

The CLI also prints the final summary. It includes `cloudwatch_used: false` so
the selected path is explicit.

## Validation

```bash
uv run ruff check .
uv run pytest
uv run pyright
```

## Troubleshooting

- **No Langfuse observations:** verify the Langfuse URL and keys, then set
  `LANGFUSE_DEBUG=True`. The CLI calls `flush()` and polls for eventual
  consistency before reading the trace.
- **Trace never appears in the session:** ensure Langfuse supports session
  propagation and that both the session and trace APIs are reachable.
- **No AGENT span:** use
  `openinference-instrumentation-claude-agent-sdk>=0.1.3`; this project locks a
  tested newer version.
- **AgentCore AccessDenied:** grant `bedrock-agentcore:Evaluate` and the model
  invocation permissions needed by the evaluator.
- **Model not found:** confirm your `ANTHROPIC_BASE_URL` maps the exact model
  string `claude-sonnet-5`.

## References

- [Langfuse Claude Agent SDK integration](https://langfuse.com/integrations/frameworks/claude-agent-sdk)
- [AgentCore on-demand evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-on-demand.html)
- [AgentCore Claude Agent SDK span contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-claude-agent-sdk.html)
