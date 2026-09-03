"""Shopping agent on AgentCore Runtime — the reward source for the prompt recommendation.

Why this exists: `StartRecommendation` cannot identify Claude Agent SDK / OpenInference
spans as sessions, so the reward signal has to come from an AgentCore-native trace
source. This is the *same* shopping agent — same `SYSTEM_PROMPT`, same
`lookup_product_price` tool, same catalog, all taken from
`claude_sdk_evaluation.shopping` (vendored here as `shopping.py`) — rebuilt on Strands so
its Runtime sessions emit telemetry the recommendation service understands.

It is deliberately a Strands agent rather than a Claude Agent SDK one: the accepted
instrumentation scope is `strands.telemetry.tracer`. The model therefore differs from
the local agent's `claude-sonnet-5` (see README) — the prompt and tool surface, which
is what the optimizer reasons about, are identical.

Deploy with the `agentcore` CLI; see README "Deploy the reward-source runtime".
"""

from __future__ import annotations

import os
import sys

# `agentcore deploy` packages only this directory, so the shared definitions are vendored
# here as shopping.py by scripts/sync_runtime_shared.py. Put this directory on sys.path so
# the import resolves both in the container and when run locally.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402
from shopping import SYSTEM_PROMPT, TOOL_DESCRIPTION, TOOL_NAME, lookup_price  # noqa: E402
from strands import Agent, tool  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402

DEFAULT_RUNTIME_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
MAX_TOKENS = 512  # bound output per turn to keep demo cost low


def _setup_telemetry() -> None:
    """Ensure Strands' GenAI trajectory spans reach the AgentCore OTEL collector.

    Without those spans only the `AgentCore.Runtime.Invoke` wrapper span exists, and both
    batch evaluation and the recommendation have no agent/tool trajectory to read.

    Only call StrandsTelemetry().setup_otlp_exporter() when nothing has configured a
    TracerProvider yet. aws-opentelemetry-distro >= 0.19.0-aws installs one during
    auto-instrumentation; calling Strands' setup on top of it logs "Overriding of current
    TracerProvider is not allowed" and leaves the Strands spans attached to a provider that
    never exports, so `aws/spans` receives the wrapper span alone. Older ADOT (0.18.0-aws)
    does not install one, and there the explicit call is required.
    """
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if type(provider).__name__ == "ProxyTracerProvider":
            from strands.telemetry import StrandsTelemetry

            StrandsTelemetry().setup_otlp_exporter()
            print("[telemetry] Strands OTLP exporter configured")
        else:
            print(f"[telemetry] using existing {type(provider).__name__} from auto-instrumentation")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the agent
        print(f"[telemetry] setup skipped: {type(exc).__name__}: {exc}")


def lookup_product_price(sku: str) -> str:
    """Look up the fixed demo price for one product SKU.

    Args:
        sku: The product SKU, e.g. NOTEBOOK or PEN.
    """
    text, _is_error = lookup_price(sku)
    return text


# Strands takes the tool name from the function name and the description from the
# docstring, and a docstring has to be a literal. Assert both still match the shared
# definitions rather than letting them drift: the prompt under optimization names this
# tool, and the optimizer reads the tool names out of the traces.
assert lookup_product_price.__name__ == TOOL_NAME, "runtime tool name must equal TOOL_NAME"
assert (lookup_product_price.__doc__ or "").startswith(TOOL_DESCRIPTION), (
    "runtime tool docstring no longer matches claude_sdk_evaluation.shopping.TOOL_DESCRIPTION"
)

lookup_product_price = tool(lookup_product_price)


def build_agent() -> Agent:
    """Build the Strands shopping agent with the shared prompt and tool."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    model = BedrockModel(
        model_id=os.environ.get("AGENT_MODEL_ID", DEFAULT_RUNTIME_MODEL),
        region_name=region,
        max_tokens=MAX_TOKENS,
    )
    return Agent(model=model, tools=[lookup_product_price], system_prompt=SYSTEM_PROMPT)


_setup_telemetry()

app = BedrockAgentCoreApp()
agent = build_agent()


@app.entrypoint
def invoke(payload, context):  # noqa: ANN001 - AgentCore passes dict + ctx
    """Handle one invocation: {"prompt": "..."} -> {"response": "..."}."""
    prompt = payload.get("prompt", "Price one NOTEBOOK.")
    result = agent(prompt)
    return {"response": str(result)}


if __name__ == "__main__":
    app.run()
