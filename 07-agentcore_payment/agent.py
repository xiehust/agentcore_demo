"""
Run a Strands agent that can access a paid x402 endpoint.

Reads resource IDs from .env + .env.local (populated by setup.py).
On 402 Payment Required, AgentCorePaymentsPlugin signs and retries automatically.

Usage:
    python agent.py
    python agent.py --url https://example.com/some-paid-api
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from strands import Agent
from strands_tools import http_request

from bedrock_agentcore.payments.integrations.config import (
    AgentCorePaymentsPluginConfig,
)
from bedrock_agentcore.payments.integrations.strands.plugin import (
    AgentCorePaymentsPlugin,
)


ROOT = Path(__file__).parent


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"ERROR: {name} is not set. Run `python setup.py all` first, "
            "or fill the missing key in .env / .env.local."
        )
    return val


def _network_preferences() -> list[str]:
    raw = os.environ.get("NETWORK_PREFERENCES", "base-sepolia,eip155:84532")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _build_plugin() -> AgentCorePaymentsPlugin:
    config = AgentCorePaymentsPluginConfig(
        payment_manager_arn=_require("PAYMENT_MANAGER_ARN"),
        user_id=_require("USER_ID"),
        payment_instrument_id=_require("PAYMENT_INSTRUMENT_ID"),
        payment_session_id=_require("PAYMENT_SESSION_ID"),
        region=_require("AWS_REGION"),
        network_preferences_config=_network_preferences(),
        agent_name="agentcore-payment-demo",
    )
    return AgentCorePaymentsPlugin(config=config)


def _handle_interrupt(interrupt: Any) -> dict[str, Any]:
    """Respond to a payment-failure interrupt.

    Follows the pattern in the AgentCore Payments docs. Demo behavior:
    print a descriptive error and ask the user to fix externally. A real app
    would re-create the session/instrument in-process.
    """
    reason = getattr(interrupt, "reason", {}) or {}
    exc_type = reason.get("exceptionType", "Unknown")
    exc_msg = reason.get("exceptionMessage", "")

    hint = ""
    if exc_type == "PaymentSessionConfigurationRequired":
        hint = "\n  => Session expired or invalid. Run `python setup.py session` then retry."
    elif exc_type == "PaymentInstrumentConfigurationRequired":
        hint = "\n  => Instrument issue. Run `python setup.py instrument` then retry."

    print(f"\n[payment interrupt] {exc_type}: {exc_msg}{hint}\n")

    return {
        "interruptResponse": {
            "interruptId": interrupt.id,
            "response": f"Payment failed: {exc_type}. Abort this request.",
        }
    }


def run(paid_url: str) -> None:
    plugin = _build_plugin()

    agent = Agent(
        model='global.anthropic.claude-opus-4-6-v1',
        system_prompt=(
            "You are an agent that accesses paid HTTP APIs. "
            "When the user asks you to GET a URL, use the http_request tool. "
            "If the server requires payment, it will be handled automatically. "
            "Print the response body verbatim at the end."
        ),
        tools=[http_request],
        plugins=[plugin],
    )

    prompt = f"GET {paid_url} and show me the response body exactly."
    print(f"\n> {prompt}\n")

    result = agent(prompt)
    while getattr(result, "stop_reason", None) == "interrupt":
        responses = [_handle_interrupt(i) for i in getattr(result, "interrupts", [])]
        if not responses:
            break
        result = agent(responses)

    print("\n=== Agent response ===")
    print(getattr(result, "message", result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Paid x402 endpoint URL. Default: $PAID_URL.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.local", override=True)

    paid_url = args.url or os.environ.get(
        "PAID_URL", "https://drvd12nxpcyd5.cloudfront.net/market-recap"
    )
    run(paid_url)


if __name__ == "__main__":
    main()
