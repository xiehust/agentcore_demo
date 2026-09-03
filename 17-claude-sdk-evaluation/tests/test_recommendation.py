from unittest.mock import Mock, patch

import pytest

from claude_sdk_evaluation.recommendation import (
    RecommendationFailed,
    RecommendationTimeout,
    evaluator_arn,
    recommend_system_prompt,
)

SPAN = {"traceId": "a" * 32, "spanId": "b" * 16, "attributes": {}}


def completed_response() -> dict:
    return {
        "recommendationId": "rec-123",
        "recommendationArn": "arn:aws:bedrock-agentcore:us-west-2:123:recommendation/rec-123",
        "status": "COMPLETED",
        "recommendationResult": {
            "systemPromptRecommendationResult": {
                "recommendedSystemPrompt": "Use the catalog tool for every requested SKU.",
                "explanation": "Makes tool usage explicit.",
            }
        },
    }


def test_recommendation_passes_inline_spans_and_poll_result():
    client = Mock()
    client.start_recommendation.return_value = {
        "recommendationId": "rec-123",
        "status": "PENDING",
    }
    client.get_recommendation.return_value = completed_response()

    with patch(
        "claude_sdk_evaluation.recommendation.secrets.token_hex",
        side_effect=["abc123", "c" * 40],
    ):
        result = recommend_system_prompt(
            [SPAN],
            system_prompt="Original prompt",
            evaluator_id="Builtin.GoalSuccessRate",
            region="us-west-2",
            poll_seconds=0,
            client=client,
        )

    client.start_recommendation.assert_called_once_with(
        name="claude-sdk-rec-abc123",
        type="SYSTEM_PROMPT_RECOMMENDATION",
        recommendationConfig={
            "systemPromptRecommendationConfig": {
                "systemPrompt": {"text": "Original prompt"},
                "agentTraces": {"sessionSpans": [SPAN]},
                "evaluationConfig": {
                    "evaluators": [
                        {
                            "evaluatorArn": (
                                "arn:aws:bedrock-agentcore:::evaluator/"
                                "Builtin.GoalSuccessRate"
                            )
                        }
                    ]
                },
            }
        },
        clientToken="c" * 40,
    )
    client.get_recommendation.assert_called_once_with(recommendationId="rec-123")
    assert result == {
        "recommendation_id": "rec-123",
        "recommendation_arn": (
            "arn:aws:bedrock-agentcore:us-west-2:123:recommendation/rec-123"
        ),
        "status": "COMPLETED",
        "recommended_system_prompt": "Use the catalog tool for every requested SKU.",
        "explanation": "Makes tool usage explicit.",
        "evaluator_arn": (
            "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"
        ),
        "trace_source": {"type": "sessionSpans", "span_count": 1},
    }


def test_recommendation_creates_region_client_when_not_injected():
    client = Mock()
    client.start_recommendation.return_value = {
        "recommendationId": "rec-123",
        "status": "PENDING",
    }
    client.get_recommendation.return_value = completed_response()

    with patch("claude_sdk_evaluation.recommendation.boto3.client", return_value=client) as factory:
        recommend_system_prompt(
            [SPAN],
            system_prompt="Original prompt",
            evaluator_id="arn:aws:bedrock-agentcore:us-west-2:123:evaluator/custom",
            region="us-west-2",
            poll_seconds=0,
        )

    factory.assert_called_once_with("bedrock-agentcore", region_name="us-west-2")


def test_recommendation_surfaces_failed_result():
    client = Mock()
    client.start_recommendation.return_value = {
        "recommendationId": "rec-failed",
        "status": "IN_PROGRESS",
    }
    client.get_recommendation.return_value = {
        "recommendationId": "rec-failed",
        "status": "FAILED",
        "recommendationResult": {
            "systemPromptRecommendationResult": {
                "errorCode": "INVALID_TRACES",
                "errorMessage": "No valid sessions found",
            }
        },
    }

    with pytest.raises(RecommendationFailed, match="INVALID_TRACES") as captured:
        recommend_system_prompt(
            [SPAN],
            system_prompt="Original prompt",
            evaluator_id="Builtin.GoalSuccessRate",
            region="us-west-2",
            poll_seconds=0,
            client=client,
        )

    assert captured.value.recommendation_id == "rec-failed"
    assert captured.value.error_message == "No valid sessions found"


def test_recommendation_timeout_preserves_id_and_last_status():
    client = Mock()
    client.start_recommendation.return_value = {
        "recommendationId": "rec-slow",
        "status": "PENDING",
    }
    monotonic = Mock(side_effect=[0.0, 5.0])

    with pytest.raises(RecommendationTimeout, match="last status: PENDING") as captured:
        recommend_system_prompt(
            [SPAN],
            system_prompt="Original prompt",
            evaluator_id="Builtin.GoalSuccessRate",
            region="us-west-2",
            timeout_seconds=5,
            client=client,
            monotonic=monotonic,
        )

    assert captured.value.recommendation_id == "rec-slow"
    client.get_recommendation.assert_not_called()


def test_evaluator_arn_preserves_custom_arn_and_rejects_empty():
    custom = "arn:aws:bedrock-agentcore:us-west-2:123:evaluator/custom"

    assert evaluator_arn(custom) == custom
    with pytest.raises(ValueError, match="must not be empty"):
        evaluator_arn("  ")


def test_recommendation_passes_batch_evaluation_trace_source():
    client = Mock()
    client.start_recommendation.return_value = completed_response()
    batch_arn = (
        "arn:aws:bedrock-agentcore:us-west-2:123:batch-evaluate/eval-completed"
    )

    result = recommend_system_prompt(
        None,
        system_prompt="Original prompt",
        evaluator_id="Builtin.GoalSuccessRate",
        region="us-west-2",
        batch_evaluation_arn=batch_arn,
        client=client,
    )

    config = client.start_recommendation.call_args.kwargs["recommendationConfig"]
    assert config["systemPromptRecommendationConfig"]["agentTraces"] == {
        "batchEvaluation": {"batchEvaluationArn": batch_arn}
    }
    assert result["status"] == "COMPLETED"
    client.get_recommendation.assert_not_called()


def test_recommendation_requires_exactly_one_trace_source():
    kwargs = {
        "system_prompt": "Original prompt",
        "evaluator_id": "Builtin.GoalSuccessRate",
        "region": "us-west-2",
        "client": Mock(),
    }

    with pytest.raises(ValueError, match="is required"):
        recommend_system_prompt(None, **kwargs)
    with pytest.raises(ValueError, match="not both"):
        recommend_system_prompt(
            [SPAN],
            batch_evaluation_arn="arn:aws:bedrock-agentcore:us-west-2:123:batch-evaluate/eval",
            **kwargs,
        )


def test_result_records_which_traces_were_analyzed():
    """The reward traces are the batch evaluation's sessions, not the inline spans."""
    client = Mock()
    client.start_recommendation.return_value = completed_response()
    batch_arn = "arn:aws:bedrock-agentcore:us-west-2:123:batch-evaluate/eval-completed"

    batch_result = recommend_system_prompt(
        None,
        system_prompt="Original prompt",
        evaluator_id="Builtin.GoalSuccessRate",
        region="us-west-2",
        batch_evaluation_arn=batch_arn,
        client=client,
    )
    assert batch_result["trace_source"] == {
        "type": "batchEvaluation",
        "batch_evaluation_arn": batch_arn,
    }

    inline_result = recommend_system_prompt(
        [SPAN, SPAN],
        system_prompt="Original prompt",
        evaluator_id="Builtin.GoalSuccessRate",
        region="us-west-2",
        client=client,
    )
    assert inline_result["trace_source"] == {"type": "sessionSpans", "span_count": 2}
