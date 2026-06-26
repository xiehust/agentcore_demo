"""Shared helpers: load demo config and build the Strands support agent.

Both the deployed entrypoint (agent/main.py) and the local scripts build the agent
through `build_agent()` so the model, tools, and prompt stay identical everywhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from agent import orders
from agent.prompts import get_active_prompt

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.json"

MAX_TOKENS = 512  # bound output per turn to keep demo cost low


def load_config() -> dict[str, Any]:
    """Load config.json written by preflight.py.

    Falls back to environment variables (AGENT_MODEL_ID / AWS_REGION) when config.json
    is not present — e.g. inside the deployed AgentCore Runtime container, where the
    model id is injected via `agentcore deploy --env AGENT_MODEL_ID=...`.
    """
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text())
    model_id = os.environ.get("AGENT_MODEL_ID")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
    if model_id:
        return {"agent_model_id": model_id, "region": region}
    raise FileNotFoundError(
        f"{_CONFIG_PATH} not found and AGENT_MODEL_ID env var unset — "
        "run `uv run python preflight.py` first (local) or pass --env AGENT_MODEL_ID (deploy)."
    )


# --- Tool wrappers (thin @tool shims over the pure functions in agent/orders.py) ---


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by its ID and return status, item, and delivery details.

    Args:
        order_id: The order identifier, e.g. ORD-1001.
    """
    return orders.lookup_order(order_id)


@tool
def initiate_return(order_id: str, reason: str) -> str:
    """Start a return for an order and email the customer a return label.

    Args:
        order_id: The order identifier, e.g. ORD-1001.
        reason: Why the customer is returning the item.
    """
    return orders.initiate_return(order_id, reason)


@tool
def check_shipping_status(order_id: str) -> str:
    """Get detailed carrier/shipping status for an order, including delays.

    Args:
        order_id: The order identifier, e.g. ORD-1002.
    """
    return orders.check_shipping_status(order_id)


@tool
def apply_discount(order_id: str, discount_percent: int, reason: str) -> str:
    """Apply a percentage discount to an order and issue a refund.

    Args:
        order_id: The order identifier, e.g. ORD-1003.
        discount_percent: The discount to apply, as an integer percent.
        reason: Why the discount is being applied.
    """
    return orders.apply_discount(order_id, discount_percent, reason)


@tool
def escalate_to_human(reason: str) -> str:
    """Escalate the conversation to a human support agent.

    Args:
        reason: Why the issue needs a human.
    """
    return orders.escalate_to_human(reason)


TOOLS = [lookup_order, initiate_return, check_shipping_status, apply_discount, escalate_to_human]


def build_agent(system_prompt: str | None = None) -> Agent:
    """Construct the Strands support agent using the model from config.json."""
    cfg = load_config()
    model = BedrockModel(
        model_id=cfg["agent_model_id"],
        region_name=cfg["region"],
        max_tokens=MAX_TOKENS,
    )
    return Agent(
        model=model,
        tools=TOOLS,
        system_prompt=system_prompt or get_active_prompt(),
    )


def tools_used(agent: Agent) -> list[str]:
    """Inspect an agent's message history and return the tool names that were invoked."""
    used: list[str] = []
    for msg in getattr(agent, "messages", []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            continue
        for block in content:
            if isinstance(block, dict) and "toolUse" in block:
                name = block["toolUse"].get("name")
                if name:
                    used.append(name)
    return used
