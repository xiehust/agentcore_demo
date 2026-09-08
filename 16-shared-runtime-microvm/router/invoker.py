"""AgentCore data-plane access: warm-up, streaming invoke, and session stop.

The Router never trusts HTTP 200 alone (§15.3): callers must see a final
`complete` SSE event from the agent container before treating a request as
successful.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from config import Settings

log = logging.getLogger("router.invoker")


class IncompleteInvocationError(RuntimeError):
    """HTTP 200 without a final `complete` event."""


class WarmupError(RuntimeError):
    pass


def _is_retryable_conflict(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata") or {}
        error = response.get("Error") or {}
        if metadata.get("HTTPStatusCode") == 409:
            return True
        if error.get("Code") == "RetryableConflictException":
            return True
    return type(exc).__name__ == "RetryableConflictException"


def retry_conflicts(
    operation: Callable[[], Any],
    *,
    attempts: int = 6,
    initial_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry only AgentCore provisioning/teardown HTTP 409 conflicts (§15.2)."""
    for index in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if not _is_retryable_conflict(exc) or index + 1 >= attempts:
                raise
            delay = min(initial_delay * (2**index), 8.0)
            log.info("409 conflict; retrying in %.1fs (attempt %d)", delay, index + 1)
            sleep(delay)
    raise AssertionError("unreachable")


def iter_sse_events(chunks: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    """Incrementally parse `data:` SSE frames from a byte stream."""
    buffer = ""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            event = _parse_frame(frame)
            if event is not None:
                yield event
    if buffer.strip():
        event = _parse_frame(buffer)
        if event is not None:
            yield event


def _parse_frame(frame: str) -> dict[str, Any] | None:
    data_lines: list[str] = []
    for line in frame.split("\n"):
        if line.startswith(":") or not line:
            continue
        if line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return {"event": "malformed", "raw": data[:200]}
    if not isinstance(event, dict):
        return {"event": "malformed", "raw": data[:200]}
    return event


class AgentCoreInvoker:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or boto3.client(
            "bedrock-agentcore",
            region_name=settings.region,
            config=Config(
                connect_timeout=30,
                read_timeout=settings.invoke_read_timeout_s,
                retries={"total_max_attempts": 1},
                max_pool_connections=64,
            ),
        )

    def _base(self, session_id: str) -> dict[str, Any]:
        request: dict[str, Any] = {
            "agentRuntimeArn": self.settings.runtime_arn,
            "runtimeSessionId": session_id,
        }
        if self.settings.runtime_qualifier:
            request["qualifier"] = self.settings.runtime_qualifier
        return request

    def warmup(self, session_id: str) -> dict[str, Any]:
        """Start (or resume) the microVM and return the container fingerprint."""
        request = self._base(session_id)
        request.update(
            {
                "runtimeUserId": "router-warmup",
                "payload": json.dumps(
                    {
                        "warmup": True,
                        "user_id": "router-warmup",
                        "request_id": f"warmup-{uuid.uuid4().hex}",
                    }
                ).encode("utf-8"),
                "contentType": "application/json",
                "accept": "application/json",
            }
        )
        response = retry_conflicts(lambda: self.client.invoke_agent_runtime(**request))
        body = response.get("response")
        raw = body.read() if hasattr(body, "read") else body
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise WarmupError(f"warmup returned non-JSON body: {raw[:120]!r}") from exc
        if not isinstance(payload, dict) or payload.get("warmup") is not True:
            raise WarmupError(f"unexpected warmup payload: {str(payload)[:200]}")
        if payload.get("event") == "error":
            raise WarmupError(str(payload.get("message") or "warm-up refused by the container"))
        return payload

    def stream_invoke(
        self,
        session_id: str,
        *,
        user_id: str,
        request_id: str,
        prompt: str,
        reset: bool,
    ) -> Iterator[dict[str, Any]]:
        """Yield application SSE events as they arrive from the container."""
        request = self._base(session_id)
        request.update(
            {
                "runtimeUserId": user_id,
                "payload": json.dumps(
                    {
                        "prompt": prompt,
                        "user_id": user_id,
                        "reset": reset,
                        "request_id": request_id,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                "contentType": "application/json",
                "accept": "text/event-stream",
            }
        )
        response = retry_conflicts(lambda: self.client.invoke_agent_runtime(**request))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status is not None and status != 200:
            raise RuntimeError(f"InvokeAgentRuntime status {status}")
        body = response.get("response")
        if hasattr(body, "iter_chunks"):
            chunks: Iterator[bytes] = body.iter_chunks(chunk_size=1024)
        else:
            chunks = iter([body if isinstance(body, bytes) else bytes(body or b"")])
        yield from iter_sse_events(chunks)

    def stop_session(self, session_id: str) -> dict[str, Any]:
        request = self._base(session_id)
        request["clientToken"] = str(uuid.uuid4())
        try:
            response = retry_conflicts(lambda: self.client.stop_runtime_session(**request))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            return {"success": False, "error": code or str(exc)}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return {
            "success": status == 200,
            "status_code": status,
            "runtime_session_id": response.get("runtimeSessionId"),
        }
