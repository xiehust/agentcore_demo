from pathlib import Path

from claude_sdk_evaluation import shopping
from claude_sdk_evaluation.agent import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parent.parent


def test_vendored_shopping_module_matches_source():
    """The deployed runtime must run the exact prompt the recommendation optimizes.

    `agentcore deploy` packages only runtime_agent/, so shopping.py is vendored there. A
    stale copy would mean the reward sessions ran a different prompt than the one submitted
    to StartRecommendation, silently invalidating the recommendation.
    """
    source = ROOT / "src" / "claude_sdk_evaluation" / "shopping.py"
    vendored = ROOT / "runtime_agent" / "shopping.py"

    assert vendored.exists(), "run scripts/sync_runtime_shared.py"
    assert vendored.read_bytes() == source.read_bytes(), (
        "runtime_agent/shopping.py is stale; run scripts/sync_runtime_shared.py"
    )


def test_local_agent_uses_the_shared_prompt():
    assert SYSTEM_PROMPT is shopping.SYSTEM_PROMPT


def test_lookup_price_is_deterministic_and_flags_unknown_skus():
    text, is_error = shopping.lookup_price("notebook")
    assert text == "SKU NOTEBOOK: Notebook costs $4.50"
    assert is_error is False

    text, is_error = shopping.lookup_price("WIDGET")
    assert is_error is True
    assert "Unknown SKU: WIDGET" in text
