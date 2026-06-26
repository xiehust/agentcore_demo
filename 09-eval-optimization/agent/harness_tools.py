"""Inline-function tool definitions for the managed AgentCore Harness.

The managed harness declares tools by name + description + JSON-schema (`inlineFunction`).
At invoke time the harness emits a `toolUse` and the CLIENT executes it and returns a
`toolResult` (the client-side tool loop in scripts/harness_agent.py). The actual logic is
the same deterministic order functions used by the Strands agent (agent/orders.py), so the
two harness flavors stay behaviorally identical.
"""
from __future__ import annotations

from agent import orders

# Each entry is a CreateHarness/UpdateHarness `tools` item.
TOOL_SPECS = [
    {
        "type": "inline_function",
        "name": "lookup_order",
        "config": {"inlineFunction": {
            "description": "Look up an order by its ID and return status, item, and delivery details.",
            "inputSchema": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "Order id, e.g. ORD-1001"}},
                "required": ["order_id"],
            },
        }},
    },
    {
        "type": "inline_function",
        "name": "initiate_return",
        "config": {"inlineFunction": {
            "description": "Start a return for an order and email the customer a return label.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order id, e.g. ORD-1001"},
                    "reason": {"type": "string", "description": "Why the customer is returning the item"},
                },
                "required": ["order_id", "reason"],
            },
        }},
    },
    {
        "type": "inline_function",
        "name": "check_shipping_status",
        "config": {"inlineFunction": {
            "description": "Get detailed carrier/shipping status for an order, including delays.",
            "inputSchema": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "Order id, e.g. ORD-1002"}},
                "required": ["order_id"],
            },
        }},
    },
    {
        "type": "inline_function",
        "name": "apply_discount",
        "config": {"inlineFunction": {
            "description": "Apply a percentage discount to an order and issue a refund.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order id, e.g. ORD-1003"},
                    "discount_percent": {"type": "integer", "description": "Discount percent to apply"},
                    "reason": {"type": "string", "description": "Why the discount is applied"},
                },
                "required": ["order_id", "discount_percent", "reason"],
            },
        }},
    },
    {
        "type": "inline_function",
        "name": "escalate_to_human",
        "config": {"inlineFunction": {
            "description": "Escalate the conversation to a human support agent.",
            "inputSchema": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "Why a human is needed"}},
                "required": ["reason"],
            },
        }},
    },
]


def dispatch(name: str, args: dict) -> str:
    """Execute a tool call by name against the deterministic order logic."""
    args = args or {}
    if name == "lookup_order":
        return orders.lookup_order(args.get("order_id", ""))
    if name == "initiate_return":
        return orders.initiate_return(args.get("order_id", ""), args.get("reason", ""))
    if name == "check_shipping_status":
        return orders.check_shipping_status(args.get("order_id", ""))
    if name == "apply_discount":
        return orders.apply_discount(
            args.get("order_id", ""), int(args.get("discount_percent", 0)), args.get("reason", "")
        )
    if name == "escalate_to_human":
        return orders.escalate_to_human(args.get("reason", ""))
    return f"Unknown tool: {name}"
