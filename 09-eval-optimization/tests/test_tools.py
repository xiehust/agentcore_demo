"""Unit tests for the deterministic order tool logic (agent/orders.py)."""
from __future__ import annotations

from agent import orders


def test_lookup_known_order():
    out = orders.lookup_order("ORD-1001")
    assert "delivered" in out
    assert "Blue T-Shirt (L)" in out
    assert "ORD-1001" in out


def test_lookup_unknown_order():
    out = orders.lookup_order("ORD-9999")
    assert "not found" in out.lower()


def test_initiate_return_known():
    out = orders.initiate_return("ORD-1001", "wrong size")
    assert "Return initiated for ORD-1001" in out
    assert "wrong size" in out


def test_initiate_return_unknown():
    out = orders.initiate_return("ORD-9999", "x")
    assert "not found" in out.lower()


def test_check_shipping_delay_mentions_discount_policy():
    out = orders.check_shipping_status("ORD-1003")
    assert "delayed" in out.lower()
    assert f"{orders.DELAY_DISCOUNT_PERCENT}%" in out


def test_check_shipping_unknown():
    out = orders.check_shipping_status("ORD-9999")
    assert "no active shipment" in out.lower()


def test_apply_discount_known():
    out = orders.apply_discount("ORD-1003", 15, "late delivery")
    assert "15%" in out
    assert "ORD-1003" in out


def test_apply_discount_unknown():
    out = orders.apply_discount("ORD-9999", 15, "x")
    assert "not found" in out.lower()


def test_escalate():
    out = orders.escalate_to_human("angry customer")
    assert "human agent" in out.lower()
    assert "angry customer" in out
