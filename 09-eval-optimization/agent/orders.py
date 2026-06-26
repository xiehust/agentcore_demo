"""Deterministic Acme Store order data + tool logic.

Kept free of the Strands `@tool` decorator so it is trivially unit-testable and so
the agent's tool behavior is reproducible ground truth for evaluation.
"""
from __future__ import annotations

ORDERS: dict[str, dict] = {
    "ORD-1001": {"status": "delivered", "item": "Blue T-Shirt (L)", "delivered": "2026-03-28", "total": "$29.99"},
    "ORD-1002": {"status": "in_transit", "item": "Running Shoes (10)", "shipped": "2026-03-30", "est_delivery": "2026-04-05", "total": "$89.99"},
    "ORD-1003": {"status": "delayed", "item": "Wireless Headphones", "shipped": "2026-03-25", "est_delivery": "2026-03-29", "days_late": 5, "total": "$59.99"},
    "ORD-1004": {"status": "processing", "item": "Yoga Mat", "ordered": "2026-04-02", "total": "$34.99"},
    "ORD-1005": {"status": "delivered", "item": "Coffee Maker", "delivered": "2026-03-20", "total": "$149.99"},
}

# Acme Store policy: orders delayed 3+ days qualify for a 15% goodwill discount.
DELAY_DISCOUNT_THRESHOLD_DAYS = 3
DELAY_DISCOUNT_PERCENT = 15


def lookup_order(order_id: str) -> str:
    """Return an order's status, item, and delivery details, or a not-found message."""
    order = ORDERS.get(order_id)
    if order is None:
        return f"Order {order_id} not found. Please check the order ID."
    return str({"order_id": order_id, **order})


def initiate_return(order_id: str, reason: str) -> str:
    """Initiate a return and send a return label to the customer."""
    if order_id not in ORDERS:
        return f"Cannot start a return: order {order_id} not found."
    return (
        f"Return initiated for {order_id}. Reason: {reason}. "
        "A return label was emailed to the customer. Please ship within 14 days."
    )


def check_shipping_status(order_id: str) -> str:
    """Return detailed carrier/shipping status, including delays and any discount eligibility."""
    statuses = {
        "ORD-1002": "Package is with the carrier, currently in Portland OR. On schedule for April 5.",
        "ORD-1003": (
            "Package delayed at the Memphis TN distribution center. Original delivery March 29, "
            f"now {ORDERS['ORD-1003']['days_late']} days late. Acme policy: orders delayed "
            f"{DELAY_DISCOUNT_THRESHOLD_DAYS}+ days qualify for a {DELAY_DISCOUNT_PERCENT}% discount."
        ),
    }
    return statuses.get(order_id, f"No active shipment found for {order_id}.")


def apply_discount(order_id: str, discount_percent: int, reason: str) -> str:
    """Apply a percentage discount to an order and issue a refund for the difference."""
    if order_id not in ORDERS:
        return f"Cannot apply a discount: order {order_id} not found."
    return (
        f"Applied a {discount_percent}% discount to {order_id}. Reason: {reason}. "
        "The refund will appear in 3-5 business days."
    )


def escalate_to_human(reason: str) -> str:
    """Escalate the conversation to a human support agent."""
    return f"Escalated to a human agent. Reason: {reason}. Estimated wait time: 3 minutes."
