#!/usr/bin/env python3
"""Minimal, self-contained AgentCore Runtime session + command client.

Follows the repository contract in
``.trellis/spec/backend/agentcore-runtime-command.md``:

* every event of the ``InvokeAgentRuntimeCommand`` stream is consumed and
  validated (exactly one ``contentStart``/``contentStop``, ``COMPLETED``,
  ``exitCode == 0``) before stdout is trusted;
* shell scripts are transported as base64 and executed through an explicit
  ``/bin/bash -c`` wrapper (the API does not implicitly evaluate shell syntax);
* only HTTP 409 provisioning conflicts are retried, with bounded backoff;
* ``StopRuntimeSession`` is always attempted from ``finally`` unless the caller
  explicitly asks to keep the (billable) session for debugging.

boto3 is imported lazily so the pure parsing helpers stay unit-testable.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any, Callable, Iterable

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9](?:-*[A-Za-z0-9])*$")

EXCEPTION_EVENTS = frozenset(
    {
        "accessDeniedException",
        "internalServerException",
        "resourceNotFoundException",
        "serviceQuotaExceededException",
        "throttlingException",
        "validationException",
        "runtimeClientError",
    }
)


class CommandError(RuntimeError):
    """Raised when a command did not unambiguously succeed."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        detail = (result.get("stderr") or "").strip()[:300]
        super().__init__(f"{result.get('error')}: {detail}" if detail else result.get("error"))


class SessionStopError(RuntimeError):
    """Raised when StopRuntimeSession did not confirm the requested session."""


def validate_session_id(session_id: str) -> str:
    if not (33 <= len(session_id) <= 256) or not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("runtimeSessionId must be 33..256 chars of [A-Za-z0-9-]")
    return session_id


def new_session_id(prefix: str = "probe") -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "-", prefix).strip("-")[:32] or "session"
    return validate_session_id(f"{clean}-{uuid.uuid4().hex}")


def encode_shell_script(script: str) -> str:
    """Wrap a *code-owned* shell script for InvokeAgentRuntimeCommand.

    Never pass user/prompt-derived text into this function.
    """
    if not script.strip():
        raise ValueError("script must be non-empty")
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    pipeline = f"printf '%s' '{encoded}' | base64 -d | /bin/bash"
    return f"/bin/bash -c {json.dumps(pipeline)}"


def parse_command_stream(stream: Iterable[Any]) -> dict[str, Any]:
    """Fold an event stream into stdout/stderr and structural findings."""
    stdout: list[str] = []
    stderr: list[str] = []
    starts = 0
    stop: dict[str, Any] | None = None
    problems: list[str] = []
    exceptions: list[str] = []
    for event in stream:
        if not isinstance(event, dict) or len(event) != 1:
            problems.append("malformed event")
            continue
        name, detail = next(iter(event.items()))
        if name in EXCEPTION_EVENTS:
            exceptions.append(f"{name}: {detail}")
            continue
        if name != "chunk" or not isinstance(detail, dict) or len(detail) != 1:
            problems.append(f"unknown event {name}")
            continue
        kind, content = next(iter(detail.items()))
        if kind == "contentStart":
            starts += 1
            if starts > 1 or stop is not None:
                problems.append("contentStart out of order")
        elif kind == "contentDelta":
            if starts != 1 or stop is not None:
                problems.append("contentDelta outside start/stop")
            stdout.append(str(content.get("stdout", "")))
            stderr.append(str(content.get("stderr", "")))
        elif kind == "contentStop":
            if starts != 1 or stop is not None:
                problems.append("contentStop out of order / duplicate")
            stop = dict(content)
        else:
            problems.append(f"unknown chunk {kind}")
    return {
        "stdout": "".join(stdout),
        "stderr": "".join(stderr),
        "content_start_count": starts,
        "content_stop": stop,
        "protocol_errors": problems,
        "stream_exceptions": exceptions,
    }


def command_error(result: dict[str, Any]) -> str | None:
    if result.get("api_status") != 200:
        return f"API status {result.get('api_status')!r} != 200"
    if result.get("stream_exceptions"):
        return "event stream reported an exception"
    if result.get("protocol_errors"):
        return "event stream violated start/delta/stop ordering"
    if result.get("content_start_count") != 1:
        return "expected exactly one contentStart"
    if result.get("runtime_session_id") != result.get("expected_session_id"):
        return "response did not confirm the requested session"
    stop = result.get("content_stop")
    if not isinstance(stop, dict):
        return "stream ended without contentStop"
    if stop.get("status") != "COMPLETED":
        return f"status {stop.get('status')!r} != COMPLETED"
    if stop.get("exitCode") != 0:
        return f"exitCode {stop.get('exitCode')!r} != 0"
    return None


def _is_conflict(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        if (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") == 409:
            return True
        if (response.get("Error") or {}).get("Code") == "RetryableConflictException":
            return True
    return type(exc).__name__ == "RetryableConflictException"


def retry_conflicts(op: Callable[[], Any], *, attempts: int = 6, sleep=time.sleep) -> Any:
    for index in range(attempts):
        try:
            return op()
        except Exception as exc:  # noqa: BLE001 - only conflicts are retried
            if not _is_conflict(exc) or index + 1 >= attempts:
                raise
            sleep(min(0.5 * (2**index), 5.0))
    raise AssertionError("unreachable")


def _status(response: dict[str, Any]) -> int | None:
    status = response.get("statusCode")
    if status is None:
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return status if isinstance(status, int) else None


class RuntimeSession:
    """One AgentCore Runtime session. Use as a context manager."""

    def __init__(self, runtime_arn: str, region: str, session_id: str | None = None,
                 *, qualifier: str = "DEFAULT", keep_session: bool = False,
                 read_timeout: int = 900):
        import boto3
        from botocore.config import Config

        self.runtime_arn = runtime_arn
        self.session_id = validate_session_id(session_id or new_session_id())
        self.qualifier = qualifier
        self.keep_session = keep_session
        self.client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=Config(connect_timeout=30, read_timeout=read_timeout,
                          retries={"total_max_attempts": 1}),
        )
        self.stop_result: dict[str, Any] | None = None

    def _base(self) -> dict[str, Any]:
        return {
            "agentRuntimeArn": self.runtime_arn,
            "runtimeSessionId": self.session_id,
            "qualifier": self.qualifier,
        }

    def warm_up(self, payload: dict[str, Any] | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        """Activate the session; InvokeAgentRuntimeCommand requires an active session.

        ``user_id`` sets X-Amzn-Bedrock-AgentCore-Runtime-User-Id. With SigV4 inbound auth the
        platform injects the workload access token (needed by AgentCore Identity calls inside the
        agent) only when this header is present.
        """
        request = self._base()
        request.update(
            {
                "payload": json.dumps(payload or {"prompt": "ping"}).encode("utf-8"),
                "contentType": "application/json",
                "accept": "application/json, text/event-stream",
            }
        )
        if user_id:
            request["runtimeUserId"] = user_id
        response = retry_conflicts(lambda: self.client.invoke_agent_runtime(**request))
        body = response.get("response")
        text = body.read().decode("utf-8", "replace") if hasattr(body, "read") else str(body)
        return {"api_status": _status(response), "body_preview": text[:500]}

    def command(self, command: str, *, timeout: int = 60) -> dict[str, Any]:
        if not 1 <= len(command.encode("utf-8")) <= 65536:
            raise ValueError("command must be 1..65536 UTF-8 bytes")
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout must be 1..3600 seconds")
        request = self._base()
        request.update(
            {
                "contentType": "application/json",
                "accept": "application/vnd.amazon.eventstream",
                "body": {"command": command, "timeout": timeout},
            }
        )
        response = retry_conflicts(lambda: self.client.invoke_agent_runtime_command(**request))
        result = {
            "api_status": _status(response),
            "runtime_session_id": response.get("runtimeSessionId"),
            "expected_session_id": self.session_id,
            **parse_command_stream(response.get("stream", ())),
        }
        result["error"] = command_error(result)
        result["success"] = result["error"] is None
        return result

    def run_script(self, script: str, *, timeout: int = 120, require_success: bool = True) -> dict[str, Any]:
        result = self.command(encode_shell_script(script), timeout=timeout)
        if require_success and not result["success"]:
            raise CommandError(result)
        return result

    def stop(self) -> dict[str, Any]:
        request = self._base()
        request["clientToken"] = str(uuid.uuid4())
        response = retry_conflicts(lambda: self.client.stop_runtime_session(**request))
        result = {
            "status_code": _status(response),
            "runtime_session_id": response.get("runtimeSessionId"),
        }
        result["success"] = result["status_code"] == 200 and result["runtime_session_id"] == self.session_id
        if not result["success"]:
            raise SessionStopError(str(result))
        return result

    def __enter__(self) -> "RuntimeSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self.keep_session:
            self.stop_result = {
                "attempted": False,
                "warning": "session retained for debugging; compute may remain billable",
            }
            return
        try:
            self.stop_result = {"attempted": True, **self.stop()}
        except Exception as exc:  # noqa: BLE001 - cleanup failure must be reported, not raised
            self.stop_result = {"attempted": True, "success": False, "error": f"{type(exc).__name__}: {exc}"}
