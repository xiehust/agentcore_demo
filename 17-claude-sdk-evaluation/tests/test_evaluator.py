from unittest.mock import Mock, patch

from claude_sdk_evaluation.evaluator import evaluate_session_spans


def test_evaluate_passes_langfuse_spans_directly_to_agentcore():
    span = {"traceId": "a" * 32, "spanId": "b" * 16, "attributes": {}}
    client = Mock()
    client.evaluate.return_value = {"evaluationResults": [{"value": 1.0}]}

    with patch("claude_sdk_evaluation.evaluator.boto3.client", return_value=client) as factory:
        result = evaluate_session_spans(
            [span], evaluator_ids=["Builtin.Helpfulness"], region="us-west-2"
        )

    factory.assert_called_once_with("bedrock-agentcore", region_name="us-west-2")
    client.evaluate.assert_called_once_with(
        evaluatorId="Builtin.Helpfulness",
        evaluationInput={"sessionSpans": [span]},
    )
    assert result == {"Builtin.Helpfulness": [{"value": 1.0}]}
