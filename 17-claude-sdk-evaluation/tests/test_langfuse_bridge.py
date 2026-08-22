from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from claude_sdk_evaluation.langfuse_bridge import observations_to_spans


def observation(**overrides):
    start = datetime(2026, 1, 2, tzinfo=UTC)
    values = {
        "id": "0123456789abcdef",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "type": "AGENT",
        "name": "ClaudeAgentSDK.ClaudeSDKClient.receive_response",
        "start_time": start,
        "end_time": start + timedelta(seconds=1),
        "input": "What does it cost?",
        "output": "It costs $12.75.",
        "model": "claude-sonnet-5",
        "metadata": {},
        "usage_details": {"input": 10, "output": 5},
        "level": "DEFAULT",
        "parent_observation_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_converts_agent_observation_to_unified_span():
    spans = observations_to_spans([observation()], session_id="session-1")

    assert len(spans) == 1
    span = spans[0]
    assert span["traceId"] == "0123456789abcdef0123456789abcdef"
    assert span["scope"]["name"] == "openinference.instrumentation.claude_agent_sdk"
    assert span["attributes"] == {
        "openinference.span.kind": "AGENT",
        "session.id": "session-1",
        "input.value": "What does it cost?",
        "input.mime_type": "text/plain",
        "output.value": "It costs $12.75.",
        "output.mime_type": "text/plain",
        "llm.system": "anthropic",
        "llm.model_name": "claude-sonnet-5",
        "llm.token_count.prompt": 10,
        "llm.token_count.completion": 5,
    }


def test_converts_tool_and_ignores_unrelated_span():
    tool = observation(
        id="fedcba9876543210",
        type="TOOL",
        name="mcp__catalog__lookup_product_price",
        input={"sku": "PEN"},
        output=[{"type": "text", "text": "PEN costs $1.25"}],
        model=None,
        metadata={"tool.id": "toolu_123"},
        parent_observation_id="0123456789abcdef",
        usage_details={},
    )
    unrelated = observation(type="SPAN", name="wrapper")

    spans = observations_to_spans([unrelated, tool], session_id="session-2")

    assert len(spans) == 1
    span = spans[0]
    assert span["parentSpanId"] == "0123456789abcdef"
    assert span["attributes"]["openinference.span.kind"] == "TOOL"
    assert span["attributes"]["tool.name"] == "mcp__catalog__lookup_product_price"
    assert span["attributes"]["tool.id"] == "toolu_123"
    assert span["attributes"]["input.value"] == '{"sku":"PEN"}'
    assert span["attributes"]["output.mime_type"] == "application/json"


def test_accepts_openinference_kind_from_metadata():
    span_observation = observation(
        type="SPAN",
        metadata={"openinference.span.kind": "AGENT", "llm.model_name": "custom-model"},
        model=None,
    )

    spans = observations_to_spans([span_observation], session_id="session-3")

    assert spans[0]["attributes"]["llm.model_name"] == "custom-model"


def test_preserves_datetime_microseconds_as_exact_nanoseconds():
    start = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

    spans = observations_to_spans([observation(start_time=start)], session_id="session-4")

    assert spans[0]["startTimeUnixNano"] == 1767323045123456000


def test_reads_session_and_trace_back_from_langfuse():
    from unittest.mock import Mock

    from claude_sdk_evaluation.langfuse_bridge import read_session_span_logs

    trace_id = "0123456789abcdef0123456789abcdef"
    session_get = Mock(return_value=SimpleNamespace(traces=[SimpleNamespace(id=trace_id)]))
    trace_get = Mock(return_value=SimpleNamespace(observations=[observation(trace_id=trace_id)]))
    langfuse = SimpleNamespace(
        api=SimpleNamespace(
            sessions=SimpleNamespace(get=session_get),
            trace=SimpleNamespace(get=trace_get),
        )
    )

    logs = read_session_span_logs(
        langfuse,
        session_id="session-5",
        trace_id=trace_id,
        timeout_seconds=1,
        poll_seconds=0,
    )

    session_get.assert_called_once_with(session_id="session-5")
    trace_get.assert_called_once_with(trace_id=trace_id)
    assert logs.session_id == "session-5"
    assert logs.trace_id == trace_id
    assert len(logs.spans) == 1
