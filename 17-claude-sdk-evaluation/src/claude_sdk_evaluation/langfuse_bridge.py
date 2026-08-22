"""Read Langfuse observations and convert them to AgentCore session span logs."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

INSTRUMENTATION_SCOPE = "openinference.instrumentation.claude_agent_sdk"
INSTRUMENTATION_VERSION = version("openinference-instrumentation-claude-agent-sdk")
_SUPPORTED_KINDS = {"AGENT", "TOOL"}


@dataclass(frozen=True)
class SessionSpanLogs:
    """Langfuse identifiers plus spans accepted by AgentCore Evaluate."""

    session_id: str
    trace_id: str
    spans: list[dict[str, Any]]


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).upper()


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _mime_type(value: Any) -> str:
    return "text/plain" if value is None or isinstance(value, str) else "application/json"


def _unix_nanos(value: datetime | None) -> int:
    timestamp = value or datetime.now(tz=UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    delta = timestamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _metadata(observation: Any) -> Mapping[str, Any]:
    metadata = getattr(observation, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _observation_kind(observation: Any) -> str | None:
    metadata = _metadata(observation)
    kind = metadata.get("openinference.span.kind")
    if kind is not None and str(kind).upper() in _SUPPORTED_KINDS:
        return str(kind).upper()
    observation_type = _enum_value(getattr(observation, "type", ""))
    return observation_type if observation_type in _SUPPORTED_KINDS else None


def observation_to_span(observation: Any, *, session_id: str) -> dict[str, Any] | None:
    """Convert one Langfuse observation to AgentCore's unified span representation."""
    kind = _observation_kind(observation)
    if kind is None:
        return None

    trace_id = str(observation.trace_id)
    span_id = str(observation.id)
    input_value = getattr(observation, "input", None)
    output_value = getattr(observation, "output", None)
    metadata = _metadata(observation)
    attributes: dict[str, Any] = {
        "openinference.span.kind": kind,
        "session.id": session_id,
        "input.value": _json_text(input_value),
        "input.mime_type": _mime_type(input_value),
        "output.value": _json_text(output_value),
        "output.mime_type": _mime_type(output_value),
    }

    if kind == "AGENT":
        attributes["llm.system"] = "anthropic"
        attributes["llm.model_name"] = str(
            getattr(observation, "model", None)
            or metadata.get("llm.model_name")
            or "claude-sonnet-5"
        )
    else:
        attributes["tool.name"] = str(metadata.get("tool.name") or observation.name)
        attributes["tool.id"] = str(metadata.get("tool.id") or span_id)
        attributes["tool.parameters"] = _json_text(input_value)

    usage = getattr(observation, "usage_details", None)
    if isinstance(usage, Mapping):
        input_tokens = usage.get("input") or usage.get("input_tokens")
        output_tokens = usage.get("output") or usage.get("output_tokens")
        if isinstance(input_tokens, int):
            attributes["llm.token_count.prompt"] = input_tokens
        if isinstance(output_tokens, int):
            attributes["llm.token_count.completion"] = output_tokens

    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": str(observation.name),
        "kind": "INTERNAL",
        "startTimeUnixNano": _unix_nanos(getattr(observation, "start_time", None)),
        "endTimeUnixNano": _unix_nanos(getattr(observation, "end_time", None)),
        "scope": {"name": INSTRUMENTATION_SCOPE, "version": INSTRUMENTATION_VERSION},
        "attributes": attributes,
        "status": {
            "code": "ERROR" if _enum_value(getattr(observation, "level", "")) == "ERROR" else "OK"
        },
    }
    parent_id = getattr(observation, "parent_observation_id", None)
    if parent_id:
        span["parentSpanId"] = str(parent_id)
    return span


def observations_to_spans(
    observations: Sequence[Any], *, session_id: str
) -> list[dict[str, Any]]:
    """Convert and chronologically sort Agent/Tool observations only."""
    spans = [
        span
        for observation in observations
        if (span := observation_to_span(observation, session_id=session_id)) is not None
    ]
    spans.sort(key=lambda span: span["startTimeUnixNano"])
    return spans


def _session_trace_ids(session: Any) -> set[str]:
    return {str(trace.id) for trace in getattr(session, "traces", [])}


def read_session_span_logs(
    langfuse: Any,
    *,
    session_id: str,
    trace_id: str,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 2.0,
) -> SessionSpanLogs:
    """Poll Langfuse until the session and full trace are queryable, then build spans."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            session = langfuse.api.sessions.get(session_id=session_id)
            if trace_id not in _session_trace_ids(session):
                raise LookupError(f"trace {trace_id} is not visible in session {session_id} yet")
            trace = langfuse.api.trace.get(trace_id=trace_id)
            spans = observations_to_spans(trace.observations, session_id=session_id)
            if not any(span["attributes"]["openinference.span.kind"] == "AGENT" for span in spans):
                raise LookupError("the trace does not contain an AGENT observation yet")
            return SessionSpanLogs(session_id=session_id, trace_id=trace_id, spans=spans)
        except Exception as exc:  # Langfuse API uses generated exception classes.
            last_error = exc
            time.sleep(poll_seconds)

    message = f"Langfuse trace {trace_id} was not fully queryable after {timeout_seconds:.0f}s"
    if last_error is not None:
        message += f": {last_error}"
    raise TimeoutError(message)
