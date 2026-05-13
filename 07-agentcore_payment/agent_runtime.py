"""
AgentCore Runtime entrypoint.

Same logic as agent.py, but wrapped in BedrockAgentCoreApp so it can be
deployed to AgentCore Runtime. Reads configuration from environment
variables injected by Runtime.launch(env_vars=...).

Invocation payload shape:
    {"prompt": "GET https://... and show the body"}
    {"url": "https://..."}                          # implicit prompt
    {"prompt": "...custom..."}

Response shape:
    {"result": "...text of agent response..."}
"""

from __future__ import annotations

import logging
import os
from typing import Any

# Bump plugin logs to DEBUG so we can see the 402-interception path in CloudWatch.
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("bedrock_agentcore").setLevel(logging.DEBUG)
logging.getLogger("bedrock_agentcore.payments").setLevel(logging.DEBUG)

from strands import Agent
from strands_tools import http_request

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.payments.integrations.config import (
    AgentCorePaymentsPluginConfig,
)
from bedrock_agentcore.payments.integrations.strands.plugin import (
    AgentCorePaymentsPlugin,
)


app = BedrockAgentCoreApp()

DEFAULT_URL = "https://drvd12nxpcyd5.cloudfront.net/market-recap"


def _network_preferences() -> list[str]:
    raw = os.environ.get("NETWORK_PREFERENCES", "base-sepolia,eip155:84532")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _build_agent() -> Agent:
    plugin = AgentCorePaymentsPlugin(config=AgentCorePaymentsPluginConfig(
        payment_manager_arn=os.environ["PAYMENT_MANAGER_ARN"],
        user_id=os.environ["USER_ID"],
        payment_instrument_id=os.environ["PAYMENT_INSTRUMENT_ID"],
        payment_session_id=os.environ["PAYMENT_SESSION_ID"],
        region=os.environ.get("AWS_REGION", "us-west-2"),
        network_preferences_config=_network_preferences(),
        agent_name="agentcore-payment-demo-runtime",
    ))
    return Agent(
        system_prompt=(
            "You are an agent that accesses paid HTTP APIs. "
            "When asked to GET a URL, use the http_request tool. "
            "If the server requires payment, the platform handles it "
            "automatically. Print the response body verbatim."
        ),
        tools=[http_request],
        plugins=[plugin],
    )


# Build once at cold start -- reused across invocations in the same container.
_agent = _build_agent()


def _handle_interrupt(interrupt: Any) -> dict[str, Any]:
    reason = getattr(interrupt, "reason", {}) or {}
    exc_type = reason.get("exceptionType", "Unknown")
    exc_msg = reason.get("exceptionMessage", "")
    return {
        "interruptResponse": {
            "interruptId": interrupt.id,
            "response": f"Payment failed: {exc_type}: {exc_msg}. Abort.",
        }
    }


@app.entrypoint
def handler(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = payload.get("prompt")
    if not prompt:
        url = payload.get("url", DEFAULT_URL)
        prompt = f"GET {url} and show me the response body exactly."

    result = _agent(prompt)
    while getattr(result, "stop_reason", None) == "interrupt":
        responses = [_handle_interrupt(i)
                     for i in getattr(result, "interrupts", [])]
        if not responses:
            break
        result = _agent(responses)

    message = getattr(result, "message", None)
    if isinstance(message, dict):
        content = message.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", str(message))
        else:
            text = str(message)
    else:
        text = str(message or result)

    return {"result": text}


if __name__ == "__main__":
    app.run()
