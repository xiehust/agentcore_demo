"""AgentCore system prompt recommendations from caller-provided session spans."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import boto3

_BUILTIN_EVALUATOR_ARN_PREFIX = "arn:aws:bedrock-agentcore:::evaluator/"
_TERMINAL_STATUSES = {"COMPLETED", "FAILED"}


class RecommendationFailed(RuntimeError):
    """A recommendation reached the FAILED terminal state."""

    def __init__(self, recommendation_id: str, error_code: str, error_message: str) -> None:
        self.recommendation_id = recommendation_id
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(
            f"AgentCore recommendation {recommendation_id} failed: "
            f"[{error_code}] {error_message}"
        )


class RecommendationTimeout(TimeoutError):
    """A recommendation did not reach a terminal state before the deadline."""

    def __init__(
        self, recommendation_id: str, status: str, timeout_seconds: float
    ) -> None:
        self.recommendation_id = recommendation_id
        self.status = status
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"AgentCore recommendation {recommendation_id} did not finish within "
            f"{timeout_seconds:g}s; last status: {status}"
        )

def evaluator_arn(evaluator_id: str) -> str:
    """Expand a built-in evaluator ID while preserving custom evaluator ARNs."""
    value = evaluator_id.strip()
    if not value:
        raise ValueError("recommendation evaluator ID must not be empty")
    if value.startswith("arn:"):
        return value
    return _BUILTIN_EVALUATOR_ARN_PREFIX + value


def recommend_system_prompt(
    spans: Sequence[dict[str, Any]] | None,
    *,
    system_prompt: str,
    evaluator_id: str,
    region: str,
    batch_evaluation_arn: str | None = None,
    timeout_seconds: float = 900.0,
    poll_seconds: float = 15.0,
    name: str | None = None,
    client: Any | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Start and poll a recommendation using inline spans or a batch evaluation."""
    if spans is not None and batch_evaluation_arn is not None:
        raise ValueError("provide either session spans or a batch evaluation ARN, not both")
    if spans is None and batch_evaluation_arn is None:
        raise ValueError("session spans or a batch evaluation ARN is required")
    if spans is not None and not spans:
        raise ValueError("at least one session span is required for a recommendation")
    if batch_evaluation_arn is not None and not batch_evaluation_arn.strip():
        raise ValueError("batch evaluation ARN must not be empty")
    if not system_prompt.strip():
        raise ValueError("system prompt must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("recommendation timeout must be greater than zero")
    if poll_seconds < 0:
        raise ValueError("recommendation poll interval must not be negative")

    agentcore = client or boto3.client("bedrock-agentcore", region_name=region)
    agent_traces: dict[str, Any]
    trace_source: dict[str, Any]
    if batch_evaluation_arn is not None:
        arn = batch_evaluation_arn.strip()
        agent_traces = {"batchEvaluation": {"batchEvaluationArn": arn}}
        trace_source = {"type": "batchEvaluation", "batch_evaluation_arn": arn}
    else:
        agent_traces = {"sessionSpans": list(spans or [])}
        trace_source = {"type": "sessionSpans", "span_count": len(spans or [])}

    response = agentcore.start_recommendation(
        name=name or f"claude-sdk-rec-{secrets.token_hex(6)}",
        type="SYSTEM_PROMPT_RECOMMENDATION",
        recommendationConfig={
            "systemPromptRecommendationConfig": {
                "systemPrompt": {"text": system_prompt},
                "agentTraces": agent_traces,
                "evaluationConfig": {
                    "evaluators": [{"evaluatorArn": evaluator_arn(evaluator_id)}]
                },
            }
        },
        clientToken=secrets.token_hex(20),
    )
    recommendation_id = str(response["recommendationId"])
    status = str(response["status"])
    deadline = monotonic() + timeout_seconds

    while status not in _TERMINAL_STATUSES:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RecommendationTimeout(recommendation_id, status, timeout_seconds)
        sleep(min(poll_seconds, remaining))
        response = agentcore.get_recommendation(recommendationId=recommendation_id)
        status = str(response["status"])

    result = response.get("recommendationResult", {})
    recommendation = (
        result.get("systemPromptRecommendationResult", {})
        if isinstance(result, Mapping)
        else {}
    )
    if status == "FAILED":
        error_code = str(recommendation.get("errorCode", "UNKNOWN"))
        error_message = str(recommendation.get("errorMessage", "no error message returned"))
        raise RecommendationFailed(recommendation_id, error_code, error_message)

    recommended_prompt = recommendation.get("recommendedSystemPrompt")
    if not isinstance(recommended_prompt, str) or not recommended_prompt:
        raise RuntimeError(
            f"AgentCore recommendation {recommendation_id} completed without a recommended prompt"
        )
    return {
        "recommendation_id": recommendation_id,
        "recommendation_arn": response.get("recommendationArn"),
        "status": status,
        "recommended_system_prompt": recommended_prompt,
        "explanation": recommendation.get("explanation"),
        "evaluator_arn": evaluator_arn(evaluator_id),
        "trace_source": trace_source,
    }
