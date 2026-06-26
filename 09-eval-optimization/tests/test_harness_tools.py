"""Unit tests for the managed-harness inline tool specs + dispatch (agent/harness_tools.py)."""
from __future__ import annotations

from agent import harness_tools


def test_tool_specs_shape():
    names = {t["name"] for t in harness_tools.TOOL_SPECS}
    assert names == {
        "lookup_order", "initiate_return", "check_shipping_status",
        "apply_discount", "escalate_to_human",
    }
    for t in harness_tools.TOOL_SPECS:
        assert t["type"] == "inline_function"  # snake_case enum required by the API
        inline = t["config"]["inlineFunction"]   # camelCase config key
        assert inline["description"]
        assert inline["inputSchema"]["type"] == "object"


def test_dispatch_lookup_known():
    assert "delivered" in harness_tools.dispatch("lookup_order", {"order_id": "ORD-1001"})


def test_dispatch_unknown_order():
    assert "not found" in harness_tools.dispatch("lookup_order", {"order_id": "ORD-9999"}).lower()


def test_dispatch_apply_discount_coerces_int():
    # discount_percent may arrive as a string from the model's tool input JSON
    out = harness_tools.dispatch("apply_discount", {"order_id": "ORD-1003", "discount_percent": "15", "reason": "late"})
    assert "15%" in out


def test_dispatch_unknown_tool():
    assert "Unknown tool" in harness_tools.dispatch("nope", {})
