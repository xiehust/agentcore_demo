"""System prompts for the Acme Store support agent.

BASELINE_PROMPT is *deliberately weak* — terse, no policy guidance, no tool-usage
direction. This is the starting point the Optimization phase improves on, so the
evaluation scores have measurable headroom. OPTIMIZED_PROMPT is filled in by
phase 5 (scripts/run_optimization.py) from the AgentCore Recommendations API.
"""
from __future__ import annotations

# --- Intentionally weak baseline (the "before" in the optimization loop) ---
BASELINE_PROMPT = "You are a support bot for a store. Answer the customer."

# Filled in by phase 5 from AgentCore Optimization recommendations.
OPTIMIZED_PROMPT: str | None = 'You are a support bot for a store. Answer the customer by using the appropriate tools: lookup_order for order details, check_shipping_status for delivery tracking, initiate_return for returns, and apply_discount for compensation. For read-only lookups, proceed directly. Before taking any action with real-world consequences (returns, discounts, cancellations), state the planned action in plain language and wait for explicit user approval. Do not treat silence as consent. Present results clearly and offer further assistance.'


def get_active_prompt() -> str:
    """Return the optimized prompt if one has been set, else the weak baseline."""
    return OPTIMIZED_PROMPT or BASELINE_PROMPT
