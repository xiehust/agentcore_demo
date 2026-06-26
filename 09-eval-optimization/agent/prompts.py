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
OPTIMIZED_PROMPT: str | None = None  # round-2 recommendation regressed GoalSuccessRate; not promoted (see results/comparison.json)


def get_active_prompt() -> str:
    """Return the optimized prompt if one has been set, else the weak baseline."""
    return OPTIMIZED_PROMPT or BASELINE_PROMPT
