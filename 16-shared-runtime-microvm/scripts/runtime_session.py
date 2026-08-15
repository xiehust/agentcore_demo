#!/usr/bin/env python3
"""Shared AgentCore Runtime session client and deterministic test utilities.

The module imports boto3 only when a real client is requested, so parsers and
helpers remain unit-testable without AWS credentials or network calls.
"""

from __future__ import annotations

import base64
import csv
from collections import Counter
import io
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9](?:-*[A-Za-z0-9])*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MONITOR_HEADER = (
    "epoch",
    "cpu_pct",
    "mem_used_mb",
    "mem_avail_mb",
    "load1",
    "agent_procs",
    "cgroup_memory_current_mb",
    "cgroup_memory_max_mb",
)


class RuntimeConfigError(ValueError):
    """Raised when runtime.json is missing required values."""


class SSEParseError(ValueError):
    """Raised when the application response contains malformed SSE JSON."""


class CommandExecutionError(RuntimeError):
    """Raised when an in-session command is not unambiguously successful."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        message = result.get("error") or "Runtime command failed"
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            message = f"{message}: {stderr[:300]}"
        super().__init__(message)


class SessionStopError(RuntimeError):
    """Raised when StopRuntimeSession does not confirm the requested session."""


def utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def load_runtime_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeConfigError(f"cannot read runtime config: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError(
            f"invalid JSON in runtime config: {config_path}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeConfigError("runtime config must be a JSON object")
    for key in ("region", "runtimeArn"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise RuntimeConfigError(f"runtime config requires non-empty {key}")
    if not value["runtimeArn"].startswith("arn:aws"):
        raise RuntimeConfigError("runtimeArn must be an AWS ARN")
    return value


def validate_session_id(session_id: str) -> str:
    if (
        not isinstance(session_id, str)
        or not 33 <= len(session_id) <= 256
        or not SESSION_ID_RE.fullmatch(session_id)
    ):
        raise ValueError(
            "runtimeSessionId must be 33..256 characters using letters, digits, "
            "and internal hyphens"
        )
    return session_id


def new_session_id(prefix: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "-", prefix).strip("-")[:32] or "session"
    return validate_session_id(f"{clean}-{uuid.uuid4().hex}")


def parse_levels(raw: str | list[Any]) -> list[int]:
    if isinstance(raw, str):
        text = raw.strip()
        try:
            candidate = json.loads(text) if text.startswith("[") else text.split(",")
        except json.JSONDecodeError as exc:
            raise ValueError(
                "levels must be comma-separated integers or a JSON array"
            ) from exc
    else:
        candidate = raw
    if not isinstance(candidate, list) or not candidate:
        raise ValueError("levels must be a non-empty list")
    levels: list[int] = []
    for item in candidate:
        if isinstance(item, bool):
            raise ValueError("every concurrency level must be an integer")
        if isinstance(item, int):
            level = item
        elif isinstance(item, str) and re.fullmatch(r"[+-]?\d+", item.strip()):
            level = int(item)
        else:
            raise ValueError("every concurrency level must be an integer")
        levels.append(level)
    if any(level < 1 for level in levels):
        raise ValueError("every concurrency level must be positive")
    if len(set(levels)) != len(levels):
        raise ValueError("concurrency levels must be distinct")
    return levels


def enforce_unique_workspaces(records: list[dict[str, Any]], success_key: str) -> int:
    """Add a uniqueness signal and fold it into a per-user success field."""
    workspaces = [
        record.get("workspace")
        for record in records
        if isinstance(record.get("workspace"), str) and record["workspace"]
    ]
    counts = Counter(workspaces)
    for record in records:
        workspace = record.get("workspace")
        unique = isinstance(workspace, str) and counts.get(workspace) == 1
        record["unique_workspace"] = unique
        record[success_key] = bool(record.get(success_key) and unique)
    return len(counts)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100.0
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 1)


def atomic_write_json(path: str | Path, payload: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _decode_blob(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _api_status(response: dict[str, Any]) -> int | None:
    status = response.get("statusCode")
    if status is None:
        metadata = response.get("ResponseMetadata") or {}
        status = metadata.get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _read_response_body(body: Any) -> str:
    value = body.read() if hasattr(body, "read") else body
    return _decode_blob(value)


def parse_sse(raw: str | bytes) -> list[dict[str, Any]]:
    """Parse application SSE, including multi-line ``data:`` fields."""
    text = _decode_blob(raw).replace("\r\n", "\n").replace("\r", "\n")
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise SSEParseError(f"malformed SSE data: {data[:120]!r}") from exc
        if not isinstance(event, dict):
            raise SSEParseError("SSE data must decode to a JSON object")
        events.append(event)

    for line in text.split("\n"):
        if not line:
            flush()
        elif line.startswith(":"):
            continue
        elif line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return events


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
    attempts: int = 5,
    initial_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry only AgentCore provisioning/teardown HTTP 409 conflicts."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for index in range(attempts):
        try:
            result = operation()
            if isinstance(result, dict) and _api_status(result) == 409:
                raise _ReturnedConflict(result)
            return result
        except Exception as exc:
            retryable = isinstance(exc, _ReturnedConflict) or _is_retryable_conflict(
                exc
            )
            if not retryable or index + 1 >= attempts:
                if isinstance(exc, _ReturnedConflict):
                    raise RuntimeError(
                        "operation returned HTTP 409 after retries"
                    ) from exc
                raise
            sleep(min(initial_delay * (2**index), 4.0))
    raise AssertionError("unreachable")


class _ReturnedConflict(RuntimeError):
    pass


COMMAND_EXCEPTION_EVENTS = frozenset(
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
COMMAND_CONTENT_EVENTS = frozenset({"contentStart", "contentDelta", "contentStop"})


def parse_command_stream(stream: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fold and validate an InvokeAgentRuntimeCommand event stream."""
    stdout: list[str] = []
    stderr: list[str] = []
    content_start_count = 0
    content_stop: dict[str, Any] | None = None
    exceptions: list[dict[str, str]] = []
    unknown_events: list[str] = []
    protocol_errors: list[str] = []
    state = "before_start"
    try:
        for event in stream:
            if not isinstance(event, dict):
                unknown_events.append(type(event).__name__)
                continue
            if len(event) != 1:
                unknown_events.append("empty" if not event else "+".join(sorted(event)))
                continue
            event_name, detail = next(iter(event.items()))
            if event_name == "chunk":
                if not isinstance(detail, dict) or len(detail) != 1:
                    unknown_events.append("malformed chunk")
                    continue
                content_name, content = next(iter(detail.items()))
                if content_name not in COMMAND_CONTENT_EVENTS:
                    unknown_events.append(f"chunk.{content_name}")
                    continue
                if not isinstance(content, dict):
                    protocol_errors.append(f"{content_name} payload is not an object")
                    continue
                allowed_members = {
                    "contentStart": frozenset(),
                    "contentDelta": frozenset({"stdout", "stderr"}),
                    "contentStop": frozenset({"exitCode", "status"}),
                }[content_name]
                unexpected_members = set(content) - allowed_members
                if unexpected_members:
                    protocol_errors.append(
                        f"{content_name} contained unexpected members: "
                        + ", ".join(sorted(unexpected_members))
                    )
                if content_name == "contentStart":
                    content_start_count += 1
                    if state != "before_start":
                        protocol_errors.append(
                            f"contentStart received while stream state was {state}"
                        )
                    state = "started"
                elif content_name == "contentDelta":
                    if state != "started":
                        protocol_errors.append(
                            f"contentDelta received while stream state was {state}"
                        )
                    for channel, destination in (
                        ("stdout", stdout),
                        ("stderr", stderr),
                    ):
                        if channel not in content:
                            continue
                        value = content[channel]
                        if not isinstance(value, str):
                            protocol_errors.append(
                                f"contentDelta.{channel} is not a string"
                            )
                        else:
                            destination.append(value)
                else:
                    if state != "started":
                        protocol_errors.append(
                            f"contentStop received while stream state was {state}"
                        )
                    if content_stop is not None:
                        protocol_errors.append("duplicate contentStop event")
                    if set(content) != allowed_members:
                        protocol_errors.append(
                            "contentStop must contain exactly exitCode and status"
                        )
                    if type(content.get("exitCode")) is not int:
                        protocol_errors.append("contentStop.exitCode is not an integer")
                    if content.get("status") not in {"COMPLETED", "TIMED_OUT"}:
                        protocol_errors.append(
                            "contentStop.status is not a recognized command status"
                        )
                    content_stop = dict(content)
                    state = "stopped"
                continue
            if event_name in COMMAND_EXCEPTION_EVENTS:
                if isinstance(detail, dict):
                    message = detail.get("message") or json.dumps(
                        detail, sort_keys=True, default=str
                    )
                else:
                    message = str(detail)
                exceptions.append({"type": event_name, "message": str(message)})
            else:
                unknown_events.append(event_name)
    except Exception as exc:
        exceptions.append(
            {"type": type(exc).__name__, "message": str(exc) or "stream failed"}
        )
    return {
        "stdout": "".join(stdout),
        "stderr": "".join(stderr),
        "content_start_count": content_start_count,
        "content_stop": content_stop,
        "stream_exceptions": exceptions,
        "unknown_events": unknown_events,
        "protocol_errors": protocol_errors,
    }


def _command_error(result: dict[str, Any]) -> str | None:
    if result.get("api_status") != 200:
        return f"command API status is {result.get('api_status')!r}, expected 200"
    if result.get("stream_exceptions"):
        return "command event stream reported an exception"
    if result.get("unknown_events"):
        return "command event stream contained an unknown event"
    if result.get("protocol_errors"):
        return "command event stream violated content event ordering"
    if result.get("content_start_count") != 1:
        return "command event stream must contain exactly one contentStart"
    if result.get("runtime_session_id") != result.get("expected_session_id"):
        return "command response did not confirm the requested runtime session"
    stop = result.get("content_stop")
    if not isinstance(stop, dict):
        return "command event stream ended without contentStop"
    if stop.get("status") != "COMPLETED":
        return f"command status is {stop.get('status')!r}, expected 'COMPLETED'"
    if stop.get("exitCode") != 0:
        return f"command exit code is {stop.get('exitCode')!r}, expected 0"
    return None


def create_agentcore_client(
    runtime: dict[str, Any], *, read_timeout: int, max_connections: int
):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agentcore",
        region_name=runtime["region"],
        config=Config(
            connect_timeout=30,
            read_timeout=read_timeout,
            retries={"total_max_attempts": 1},
            max_pool_connections=max_connections,
        ),
    )


class RuntimeSession:
    """One active AgentCore session shared by every virtual user in a run."""

    def __init__(
        self,
        runtime: dict[str, Any],
        session_id: str,
        client: Any,
        *,
        conflict_attempts: int = 5,
    ) -> None:
        self.runtime = runtime
        self.session_id = validate_session_id(session_id)
        self.client = client
        self.conflict_attempts = conflict_attempts

    @classmethod
    def from_config(
        cls,
        config_path: str | Path,
        session_id: str,
        *,
        read_timeout: int = 1200,
        max_connections: int = 64,
    ) -> "RuntimeSession":
        runtime = load_runtime_config(config_path)
        client = create_agentcore_client(
            runtime, read_timeout=read_timeout, max_connections=max_connections
        )
        return cls(runtime, session_id, client)

    def _base_request(self) -> dict[str, Any]:
        request = {
            "agentRuntimeArn": self.runtime["runtimeArn"],
            "runtimeSessionId": self.session_id,
        }
        qualifier = self.runtime.get("qualifier")
        if isinstance(qualifier, str) and qualifier:
            request["qualifier"] = qualifier
        return request

    def invoke(self, user_id: str, prompt: str, *, reset: bool) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        request = self._base_request()
        request.update(
            {
                "runtimeUserId": user_id,
                "payload": json.dumps(
                    {"prompt": prompt, "user_id": user_id, "reset": reset},
                    separators=(",", ":"),
                ).encode("utf-8"),
                "contentType": "application/json",
                "accept": "text/event-stream",
            }
        )
        started = time.perf_counter()
        response = retry_conflicts(
            lambda: self.client.invoke_agent_runtime(**request),
            attempts=self.conflict_attempts,
        )
        status = _api_status(response)
        events = parse_sse(_read_response_body(response.get("response", b"")))
        complete = next(
            (event for event in reversed(events) if event.get("event") == "complete"),
            {},
        )
        error_event = next(
            (event for event in events if event.get("event") == "error"), None
        )
        result_text = complete.get("result")
        success = (
            status == 200
            and bool(complete)
            and error_event is None
            and not complete.get("is_error")
            and result_text is not None
        )
        error = None
        if status != 200:
            error = f"InvokeAgentRuntime status {status!r}"
        elif error_event is not None:
            error = error_event.get("message") or "application error event"
        elif not complete:
            error = "complete SSE event missing"
        elif complete.get("is_error"):
            error = "agent returned is_error=true"
        elif result_text is None:
            error = "complete SSE event has no result"
        return {
            "success": success,
            "api_status": status,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "result": result_text,
            "error": error,
            "events": events,
            "workspace": complete.get("workspace"),
            "claude_session_id": complete.get("claude_session_id"),
            "resumed_from": complete.get("resumed_from"),
            "denied_count": complete.get("denied_count", 0),
            "instance": complete.get("instance") or (error_event or {}).get("instance"),
            "tool_call_count": sum(event.get("event") == "tool" for event in events),
        }

    def command(
        self, command: str, *, timeout: int = 60, require_success: bool = False
    ) -> dict[str, Any]:
        encoded_length = len(command.encode("utf-8")) if isinstance(command, str) else 0
        if not 1 <= encoded_length <= 65536:
            raise ValueError("command must encode to 1..65536 UTF-8 bytes")
        if not 1 <= timeout <= 3600:
            raise ValueError("command timeout must be 1..3600 seconds")
        request = self._base_request()
        request.update(
            {
                "contentType": "application/json",
                "accept": "application/vnd.amazon.eventstream",
                "body": {"command": command, "timeout": timeout},
            }
        )
        response = retry_conflicts(
            lambda: self.client.invoke_agent_runtime_command(**request),
            attempts=self.conflict_attempts,
        )
        result = {
            "api_status": _api_status(response),
            "runtime_session_id": response.get("runtimeSessionId"),
            "expected_session_id": self.session_id,
            **parse_command_stream(response.get("stream", ())),
        }
        result["error"] = _command_error(result)
        result["success"] = result["error"] is None
        if require_success and not result["success"]:
            raise CommandExecutionError(result)
        return result

    def run_shell_script(
        self, script: str, *, timeout: int = 60, require_success: bool = True
    ) -> dict[str, Any]:
        """Run code-owned shell text; callers must never pass untrusted text."""
        if not isinstance(script, str) or not script.strip():
            raise ValueError("shell script must be non-empty")
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        pipeline = f"printf '%s' '{encoded}' | base64 -d | /bin/bash"
        command = f"/bin/bash -c {json.dumps(pipeline)}"
        return self.command(command, timeout=timeout, require_success=require_success)

    def stop(self) -> dict[str, Any]:
        request = self._base_request()
        request["clientToken"] = str(uuid.uuid4())
        response = retry_conflicts(
            lambda: self.client.stop_runtime_session(**request),
            attempts=self.conflict_attempts,
        )
        status = _api_status(response)
        returned_id = response.get("runtimeSessionId")
        result = {
            "success": status == 200 and returned_id == self.session_id,
            "status_code": status,
            "runtime_session_id": returned_id,
        }
        if not result["success"]:
            raise SessionStopError(
                "StopRuntimeSession did not confirm status 200 and the requested "
                f"session ID: {result}"
            )
        return result


def cleanup_session(session: RuntimeSession, *, keep_session: bool) -> dict[str, Any]:
    """Stop a session or return an explicit billable debug-retention record."""
    if keep_session:
        return {
            "attempted": False,
            "success": False,
            "kept_for_debug": True,
            "warning": (
                "Session was intentionally retained and may remain billable "
                "until idle or lifecycle termination."
            ),
        }
    result: dict[str, Any] = {"attempted": True, "success": False}
    try:
        result.update(session.stop())
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def finalize_before_session_stop(
    before_stop: Callable[[], None], stop_session: Callable[[], None]
) -> None:
    """Run finalization without letting it prevent the session-stop attempt."""
    try:
        before_stop()
    finally:
        stop_session()


def _monitor_dir(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must contain only letters, digits, '_' and '-'")
    return f"/tmp/agentcore-loadtest/{run_id}"


_MONITOR_PROGRAM = r"""#!/usr/bin/env python3
import sys
import time
from pathlib import Path

DURATION = int(sys.argv[1])
OUTPUT = Path(sys.argv[2])


def cpu_totals():
    values = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def memory_mb():
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0])
    return (values["MemTotal"] - values["MemAvailable"]) / 1024.0, values["MemAvailable"] / 1024.0


def agent_processes():
    count = 0
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if path.read_text().strip() in {"node", "claude"}:
                count += 1
        except OSError:
            pass
    return count


def cgroup_mb(name):
    try:
        raw = Path("/sys/fs/cgroup", name).read_text().strip()
        return "" if raw == "max" else str(round(int(raw) / 1048576.0, 3))
    except (OSError, ValueError):
        return ""


OUTPUT.write_text("epoch,cpu_pct,mem_used_mb,mem_avail_mb,load1,agent_procs,cgroup_memory_current_mb,cgroup_memory_max_mb\n")
end = time.time() + DURATION
previous_total, previous_idle = cpu_totals()
time.sleep(2)
with OUTPUT.open("a", buffering=1) as output:
    while time.time() < end:
        total, idle = cpu_totals()
        delta = max(total - previous_total, 1)
        cpu = round(100.0 * (delta - (idle - previous_idle)) / delta, 1)
        previous_total, previous_idle = total, idle
        used, available = memory_mb()
        output.write(
            f"{time.time():.3f},{cpu},{used:.3f},{available:.3f},"
            f"{Path('/proc/loadavg').read_text().split()[0]},{agent_processes()},"
            f"{cgroup_mb('memory.current')},{cgroup_mb('memory.max')}\n"
        )
        time.sleep(2)
"""


def start_monitor(
    session: RuntimeSession, run_id: str, *, duration: int = 3600
) -> dict[str, Any]:
    if not 5 <= duration <= 28800:
        raise ValueError("monitor duration must be 5..28800 seconds")
    run_dir = _monitor_dir(run_id)
    program = base64.b64encode(_MONITOR_PROGRAM.encode("utf-8")).decode("ascii")
    script = f"""set -euo pipefail
umask 077
run_dir='{run_dir}'
mkdir -p "$run_dir"
printf '%s' '{program}' | base64 -d > "$run_dir/monitor.py"
chmod 700 "$run_dir/monitor.py"
nohup python3 "$run_dir/monitor.py" '{duration}' "$run_dir/monitor.csv" \
  </dev/null >"$run_dir/monitor.log" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$run_dir/monitor.pid"
for _ in $(seq 1 50); do
  [[ -f "$run_dir/monitor.csv" ]] && [[ $(wc -l < "$run_dir/monitor.csv") -ge 2 ]] && break
  kill -0 "$pid" 2>/dev/null || {{ cat "$run_dir/monitor.log" >&2; exit 1; }}
  sleep 0.1
done
[[ -f "$run_dir/monitor.csv" ]] && [[ $(wc -l < "$run_dir/monitor.csv") -ge 2 ]] \
  || {{ echo 'monitor did not produce a sample' >&2; exit 1; }}
echo "monitor_pid=$pid"
"""
    return session.run_shell_script(script, timeout=30)


def read_monitor(
    session: RuntimeSession, run_id: str, *, stop: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_dir = _monitor_dir(run_id)
    stop_block = ""
    if stop:
        stop_block = f"""
if [[ -f "$run_dir/monitor.pid" ]]; then
  pid="$(cat "$run_dir/monitor.pid")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && [[ -r "/proc/$pid/cmdline" ]]; then
    cmdline="$(tr '\\0' ' ' < "/proc/$pid/cmdline")"
    if [[ "$cmdline" == *'{run_dir}/monitor.py'* ]]; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
      if kill -0 "$pid" 2>/dev/null && [[ -r "/proc/$pid/stat" ]]; then
        process_state="$(cut -d ' ' -f 3 < "/proc/$pid/stat")"
        [[ "$process_state" == "Z" ]] || {{
          echo 'monitor process did not stop after SIGTERM' >&2
          exit 1
        }}
      fi
    fi
  fi
fi
"""
    script = f"""set -euo pipefail
run_dir='{run_dir}'
{stop_block}
test -f "$run_dir/monitor.csv"
cat "$run_dir/monitor.csv"
"""
    command_result = session.run_shell_script(script, timeout=30)
    return parse_monitor_csv(command_result["stdout"]), command_result


def parse_monitor_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != MONITOR_HEADER:
        raise ValueError(f"unexpected monitor CSV header: {reader.fieldnames!r}")
    samples: list[dict[str, Any]] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            sample = {
                "epoch": float(row["epoch"]),
                "cpu_pct": float(row["cpu_pct"]),
                "mem_used_mb": float(row["mem_used_mb"]),
                "mem_avail_mb": float(row["mem_avail_mb"]),
                "load1": float(row["load1"]),
                "agent_procs": int(row["agent_procs"]),
                "cgroup_memory_current_mb": (
                    float(row["cgroup_memory_current_mb"])
                    if row["cgroup_memory_current_mb"]
                    else None
                ),
                "cgroup_memory_max_mb": (
                    float(row["cgroup_memory_max_mb"])
                    if row["cgroup_memory_max_mb"]
                    else None
                ),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid monitor CSV row {line_number}: {row!r}") from exc
        samples.append(sample)
    return samples


def window_stats(
    samples: list[dict[str, Any]], start_epoch: float, end_epoch: float
) -> dict[str, Any]:
    window = [
        sample
        for sample in samples
        if start_epoch - 1 <= sample["epoch"] <= end_epoch + 1
    ]
    if not window:
        return {}
    cgroup_current = [
        sample["cgroup_memory_current_mb"]
        for sample in window
        if sample.get("cgroup_memory_current_mb") is not None
    ]
    cgroup_limits = sorted(
        {
            sample["cgroup_memory_max_mb"]
            for sample in window
            if sample.get("cgroup_memory_max_mb") is not None
        }
    )
    return {
        "samples": len(window),
        "cpu_avg_pct": round(
            sum(sample["cpu_pct"] for sample in window) / len(window), 1
        ),
        "cpu_max_pct": max(sample["cpu_pct"] for sample in window),
        "mem_used_max_mb": round(max(sample["mem_used_mb"] for sample in window), 1),
        "mem_avail_min_mb": round(min(sample["mem_avail_mb"] for sample in window), 1),
        "load1_max": max(sample["load1"] for sample in window),
        "agent_procs_max": max(sample["agent_procs"] for sample in window),
        "cgroup_memory_current_max_mb": (
            round(max(cgroup_current), 1) if cgroup_current else None
        ),
        "cgroup_memory_limits_mb": cgroup_limits,
    }
