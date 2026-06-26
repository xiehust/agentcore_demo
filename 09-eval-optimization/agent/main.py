"""Acme Store customer-support agent — AgentCore Runtime entrypoint.

Deployed to Amazon Bedrock AgentCore Runtime via the `agentcore` CLI (phase 3).
The agent itself is built in agent/runtime_config.build_agent() so local scripts and
the deployed runtime share one definition. Uses the deliberately weak BASELINE_PROMPT
until phase 5 sets an optimized prompt.
"""
from __future__ import annotations

import os
import sys

# Make the repo root importable so `from agent....` resolves whether this module is
# launched as `agent.main` (local) or directly as `main.py` (some deploy packagers).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

from agent.runtime_config import build_agent  # noqa: E402


def _setup_telemetry() -> None:
    """Register Strands' OTLP span exporter so the agent emits GenAI trajectory spans.

    AgentCore Evaluations reads these spans (model + tool calls) to score sessions; without
    this only the runtime wrapper span exists and evaluation has nothing to judge. Activates
    only inside the AgentCore Runtime, where the OTEL collector endpoint is configured.
    """
    try:
        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
        print("[telemetry] Strands OTLP exporter configured")
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the agent
        print(f"[telemetry] setup skipped: {type(exc).__name__}: {exc}")


_setup_telemetry()

app = BedrockAgentCoreApp()
agent = build_agent()


@app.entrypoint
def invoke(payload, context):  # noqa: ANN001 - AgentCore passes dict + ctx
    """Handle one invocation: {"prompt": "..."} -> {"response": "..."}."""
    prompt = payload.get("prompt", "Hello")
    result = agent(prompt)
    return {"response": str(result)}


if __name__ == "__main__":
    app.run()
