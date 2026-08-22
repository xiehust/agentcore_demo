"""A small deterministic tool-using agent built with Claude Agent SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from . import DEFAULT_MODEL


@dataclass(frozen=True)
class AgentRun:
    """Final user-visible result and Claude SDK session metadata."""

    response: str
    claude_session_id: str
    turns: int


@tool(
    "lookup_product_price",
    "Look up the fixed demo price for one product SKU.",
    {"sku": str},
)
async def lookup_product_price(args: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic catalog data so the trace always contains tool calls."""
    catalog = {
        "NOTEBOOK": {"name": "Notebook", "price_usd": 4.5},
        "PEN": {"name": "Pen", "price_usd": 1.25},
    }
    sku = str(args["sku"]).upper()
    product = catalog.get(sku)
    if product is None:
        return {
            "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}],
            "is_error": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": f"SKU {sku}: {product['name']} costs ${product['price_usd']:.2f}",
            }
        ]
    }


async def run_agent(prompt: str) -> AgentRun:
    """Run one tool-using turn with the required model and return the final result."""
    catalog_server = create_sdk_mcp_server(
        name="catalog",
        version="1.0.0",
        tools=[lookup_product_price],
    )
    options = ClaudeAgentOptions(
        model=DEFAULT_MODEL,
        max_turns=6,
        system_prompt=(
            "You are a concise shopping assistant. Always use lookup_product_price for each "
            "requested SKU. Show the arithmetic and end with one total in USD."
        ),
        mcp_servers={"catalog": catalog_server},
        allowed_tools=["mcp__catalog__lookup_product_price"],
    )

    assistant_text: list[str] = []
    result_message: ResultMessage | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                assistant_text.extend(
                    block.text for block in message.content if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                result_message = message

    if result_message is None:
        raise RuntimeError("Claude Agent SDK completed without a ResultMessage")
    if result_message.is_error:
        details = "; ".join(result_message.errors or []) or result_message.subtype
        raise RuntimeError(f"Claude Agent SDK run failed: {details}")

    response = result_message.result or "\n".join(assistant_text).strip()
    if not response:
        raise RuntimeError("Claude Agent SDK returned an empty response")
    return AgentRun(
        response=response,
        claude_session_id=result_message.session_id,
        turns=result_message.num_turns,
    )
