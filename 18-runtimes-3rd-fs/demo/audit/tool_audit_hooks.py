#!/usr/bin/env python3
"""Deterministic tool-call audit for Strands agents (layer L2' in docs/03).

Why a hook and not only OTEL: OTEL spans may be sampled, and the agent code
cannot *enforce* anything from a span. A ``BeforeToolCallEvent`` hook runs on
every call, can cancel the call (deny-list), redacts secrets before anything is
logged, and writes one JSON Lines record to the sink (stdout -> CloudWatch Logs
by default). The record carries the current OTEL ``trace_id``/``span_id`` so it
can be joined with AgentCore Observability spans and CloudTrail request ids.

The audit logic (``build_audit_record``, ``redact``) is pure and unit-tested;
``ToolAuditHook`` is a thin ``HookProvider`` adapter and only needs Strands at
runtime.

Usage inside an agent:

    from tool_audit_hooks import ToolAuditHook, AuditPolicy
    agent = Agent(tools=[...], hooks=[ToolAuditHook(AuditPolicy(deny_tools={"shell"}))])
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

REDACTED = "[REDACTED]"
DEFAULT_REDACT_KEYS = frozenset(
    {"authorization", "token", "access_token", "refresh_token", "password", "secret",
     "api_key", "apikey", "x-api-key", "private_key", "client_secret", "cookie"}
)


@dataclass(frozen=True)
class AuditPolicy:
    deny_tools: frozenset[str] = frozenset()
    redact_keys: frozenset[str] = DEFAULT_REDACT_KEYS
    max_value_chars: int = 512
    include_output: bool = True

    def decision(self, tool_name: str) -> str:
        return "deny" if tool_name in self.deny_tools else "allow"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"...[+{len(text) - limit} chars]"


def redact(value: Any, keys: frozenset[str] = DEFAULT_REDACT_KEYS, *, max_chars: int = 512) -> Any:
    """Recursively replace values of sensitive keys and truncate long strings."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in keys else redact(v, keys, max_chars=max_chars))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, keys, max_chars=max_chars) for v in value]
    if isinstance(value, str):
        return _truncate(value, max_chars)
    return value


def _otel_context() -> dict[str, str]:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # noqa: BLE001 - telemetry must never break auditing
        pass
    return {}


def build_audit_record(*, phase: str, tool_name: str, tool_use_id: str, tool_input: Any,
                       policy: AuditPolicy, agent_name: str = "", session_id: str = "",
                       result: Any = None, status: str | None = None, duration_ms: float | None = None,
                       otel: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Pure function: one JSON-serialisable audit record for a tool call phase."""
    if phase not in {"before", "after"}:
        raise ValueError("phase must be 'before' or 'after'")
    record: dict[str, Any] = {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": "tool_call",
        "phase": phase,
        "agent": agent_name,
        "session_id": session_id,
        "tool": tool_name,
        "tool_use_id": tool_use_id,
        "input": redact(tool_input, policy.redact_keys, max_chars=policy.max_value_chars),
        "decision": policy.decision(tool_name),
    }
    if phase == "after":
        record["status"] = status or "unknown"
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 1)
        if policy.include_output and result is not None:
            preview = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            record["output_preview"] = _truncate(preview, policy.max_value_chars)
    record.update(otel or {})
    return record


def _stdout_sink(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


try:  # Strands is only required when the hook is actually attached to an agent.
    from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
except ImportError:  # pragma: no cover - exercised only where strands is absent
    HookProvider = object  # type: ignore[assignment,misc]
    BeforeToolCallEvent = AfterToolCallEvent = HookRegistry = Any  # type: ignore[assignment,misc]


class ToolAuditHook(HookProvider):  # type: ignore[misc]
    """Strands HookProvider: audit + deny-list on every tool call."""

    def __init__(self, policy: AuditPolicy | None = None, sink: Callable[[dict[str, Any]], None] = _stdout_sink,
                 session_id: str = ""):
        self.policy = policy or AuditPolicy()
        self.sink = sink
        self.session_id = session_id
        self._started: dict[str, float] = {}

    def register_hooks(self, registry: "HookRegistry") -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def _ids(self, event: Any) -> tuple[str, str, Any, str]:
        tool_use = event.tool_use
        name = tool_use.get("name", "") if isinstance(tool_use, dict) else getattr(tool_use, "name", "")
        use_id = tool_use.get("toolUseId", "") if isinstance(tool_use, dict) else getattr(tool_use, "toolUseId", "")
        tool_input = tool_use.get("input", {}) if isinstance(tool_use, dict) else getattr(tool_use, "input", {})
        agent_name = getattr(getattr(event, "agent", None), "name", "") or ""
        return name, use_id, tool_input, agent_name

    def before_tool(self, event: Any) -> None:
        name, use_id, tool_input, agent_name = self._ids(event)
        self._started[use_id] = time.perf_counter()
        record = build_audit_record(phase="before", tool_name=name, tool_use_id=use_id, tool_input=tool_input,
                                    policy=self.policy, agent_name=agent_name, session_id=self.session_id,
                                    otel=_otel_context())
        self.sink(record)
        if record["decision"] == "deny":
            # The model receives this text as the tool result and adapts; the tool never runs.
            event.cancel_tool = f"Tool '{name}' is blocked by audit policy."

    def after_tool(self, event: Any) -> None:
        name, use_id, tool_input, agent_name = self._ids(event)
        started = self._started.pop(use_id, None)
        duration = (time.perf_counter() - started) * 1000 if started is not None else None
        result = getattr(event, "result", None)
        status = None
        if isinstance(result, dict):
            status = result.get("status")
            content = result.get("content")
        else:
            content = result
        self.sink(
            build_audit_record(phase="after", tool_name=name, tool_use_id=use_id, tool_input=tool_input,
                               policy=self.policy, agent_name=agent_name, session_id=self.session_id,
                               result=content, status=status, duration_ms=duration, otel=_otel_context())
        )
