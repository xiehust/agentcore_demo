"""Direct AgentCore on-demand evaluation calls (no CloudWatch dependency)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import boto3


def evaluate_session_spans(
    spans: Sequence[dict[str, Any]],
    *,
    evaluator_ids: Sequence[str],
    region: str,
) -> dict[str, list[dict[str, Any]]]:
    """Call Evaluate once per evaluator and return only evaluation result records."""
    if not spans:
        raise ValueError("at least one session span is required")
    if not evaluator_ids:
        raise ValueError("at least one evaluator ID is required")

    client = boto3.client("bedrock-agentcore", region_name=region)
    results: dict[str, list[dict[str, Any]]] = {}
    for evaluator_id in evaluator_ids:
        response = client.evaluate(
            evaluatorId=evaluator_id,
            evaluationInput={"sessionSpans": list(spans)},
        )
        results[evaluator_id] = response.get("evaluationResults", [])
    return results
