"""Single source of truth for the shopping agent's prompt, catalog, and tool behavior.

Two agents share these definitions:

- the local Claude Agent SDK agent (`agent.py`), whose trace goes to Langfuse and is
  scored by the on-demand `Evaluate` API;
- the Strands agent deployed to AgentCore Runtime (`runtime_agent/main.py`), whose
  sessions provide the reward signal for the system prompt recommendation.

`SYSTEM_PROMPT` is the prompt under optimization, so both agents must run it verbatim —
otherwise the recommendation optimizes a prompt that produced none of the reward traces.
This module deliberately imports nothing outside the standard library so the deployed
runtime container does not need the Claude SDK or Langfuse.
"""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You are a concise shopping assistant. Always use lookup_product_price for each "
    "requested SKU. Show the arithmetic and end with one total in USD."
)

TOOL_NAME = "lookup_product_price"
TOOL_DESCRIPTION = "Look up the fixed demo price for one product SKU."

CATALOG: dict[str, dict[str, object]] = {
    "NOTEBOOK": {"name": "Notebook", "price_usd": 4.5},
    "PEN": {"name": "Pen", "price_usd": 1.25},
    "STAPLER": {"name": "Stapler", "price_usd": 7.0},
    "MARKER": {"name": "Marker", "price_usd": 2.0},
}


def lookup_price(sku: str) -> tuple[str, bool]:
    """Return (text, is_error) for one SKU using fixed prices.

    Deterministic so every trace contains a tool call with a predictable result.
    """
    key = str(sku).upper()
    product = CATALOG.get(key)
    if product is None:
        known = ", ".join(sorted(CATALOG))
        return f"Unknown SKU: {key}. Known SKUs: {known}", True
    return f"SKU {key}: {product['name']} costs ${product['price_usd']:.2f}", False
