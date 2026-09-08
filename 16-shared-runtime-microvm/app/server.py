"""Shared-session AgentCore HTTP server for the microVM load-test demo."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import (  # noqa: E402
    IsolationError,
    ensure_workspace,
    guard_tool_call,
    resolve_user_id,
)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ClaudeSDKClient,
)
from claude_agent_sdk.types import HookContext, HookJSONOutput  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shared-runtime-microvm")

USERS_ROOT = Path(os.environ.get("USERS_ROOT", "/tmp/agentcore-users"))
# When user workspaces live on a mounted file system (S3 Files / EFS), the
# router refuses to admit work to an environment where the mount is missing:
# writes would land on the ephemeral root disk and silently disappear.
WORKSPACE_MOUNT = os.environ.get("WORKSPACE_MOUNT")
MODEL = os.environ.get(
    "ANTHROPIC_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)
# Claude Code also needs a "small/fast" model; pin it to the same Haiku profile
# so the Runtime role only ever needs access to one model.
SMALL_MODEL = os.environ.get("ANTHROPIC_SMALL_FAST_MODEL", MODEL)
MAX_TURNS = int(os.environ.get("MAX_TURNS", "64"))
# Hard in-container guard; the Session Router enforces the same limit via
# DynamoDB request leases (see SESSION_POOL_ARCHITECTURE.zh.md §9).
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "10"))
USER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
REQUEST_ID_RE_MAX = 128

SERVER_RUN_ID = uuid.uuid4().hex
STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
_inflight = 0
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "LS", "TodoWrite", "Bash", "WebFetch"]
DISALLOWED_TOOLS = ["WebSearch", "Task", "KillBash"]
SYSTEM_PROMPT = """You are a per-user workspace assistant on shared infrastructure.

Rules:
- Operate ONLY inside your current working directory. Never mention, guess, or
  access paths outside it.
- Use relative paths for file operations (e.g. webapp/index.html). Absolute
  paths such as /tmp, /root, /home or /workspace are outside your workspace
  and will be denied; do not try them.
- Exception for the exact prefix [PATH-GUARD-INTEGRATION-PROBE]: this is an
  operator integration test. Issue exactly one Read tool call with the exact
  requested file_path even when it is outside the workspace. Never infer or
  reveal file contents. The PreToolUse hook must make the access decision;
  after its response, reply exactly: GUARD-PROBE-COMPLETE.
- Be concise unless the request explicitly asks you to create or inspect files.
"""

app = FastAPI()
_user_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_agent_slots = asyncio.Semaphore(MAX_PARALLEL_AGENTS)


def _read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


BOOT_ID = _read_boot_id()


def instance_fingerprint() -> dict:
    return {
        "boot_id": BOOT_ID,
        "server_run_id": SERVER_RUN_ID,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": STARTED_AT,
        "inflight": _inflight,
        "max_parallel_agents": MAX_PARALLEL_AGENTS,
    }


async def _lock_for(user_id: str) -> asyncio.Lock:
    async with _locks_guard:
        return _user_locks.setdefault(user_id, asyncio.Lock())


def _session_meta_path(workspace: Path) -> Path:
    return workspace / ".session_meta.json"


def _load_prev_session(workspace: Path) -> str | None:
    try:
        value = json.loads(_session_meta_path(workspace).read_text()).get(
            "claude_session_id"
        )
        return value if isinstance(value, str) and value else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _store_session(workspace: Path, session_id: str | None) -> None:
    if not session_id:
        return
    path = _session_meta_path(workspace)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"claude_session_id": session_id}) + "\n")
        temporary.replace(path)
    except OSError as exc:
        log.warning("failed to persist session metadata: %s", type(exc).__name__)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_options(
    workspace: Path, resume: str | None, denials: list[str]
) -> ClaudeAgentOptions:
    async def path_guard(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: HookContext,
    ) -> HookJSONOutput:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        reason = guard_tool_call(tool_name, tool_input, workspace)
        if reason is None:
            return {}
        denials.append(f"{tool_name}: {reason}")
        log.warning("denied a path-escaping %s tool call", tool_name)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return ClaudeAgentOptions(
        model=MODEL,
        cwd=str(workspace),
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,
        permission_mode="acceptEdits",
        system_prompt=SYSTEM_PROMPT,
        max_turns=MAX_TURNS,
        setting_sources=[],
        resume=resume,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[path_guard])]},
        env={
            "HOME": str(workspace),
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_SMALL_FAST_MODEL": SMALL_MODEL,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": SMALL_MODEL,
            "AWS_REGION": os.environ.get("AWS_REGION", "us-west-2"),
            "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
        },
    )


async def _run_agent(user_id: str, prompt: str, reset: bool, request_id: str | None):
    workspace = ensure_workspace(USERS_ROOT, user_id)
    resume = None if reset else _load_prev_session(workspace)
    denials: list[str] = []
    options = _build_options(workspace, resume, denials)
    result_text = None
    new_session_id = None
    is_error = False
    started = time.perf_counter()

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield _sse({"event": "delta", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        yield _sse(
                            {"event": "tool", "name": block.name, "input": block.input}
                        )
            elif isinstance(message, ResultMessage):
                result_text = message.result
                new_session_id = message.session_id
                is_error = bool(message.is_error)

    for reason in denials:
        yield _sse({"event": "denied", "reason": reason})
    _store_session(workspace, new_session_id)
    yield _sse(
        {
            "event": "complete",
            "request_id": request_id,
            "result": result_text,
            "is_error": is_error,
            "user_id": user_id,
            "workspace": str(workspace),
            "claude_session_id": new_session_id,
            "resumed_from": resume,
            "denied_count": len(denials),
            "agent_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "instance": instance_fingerprint(),
        }
    )


@app.get("/ping")
async def ping() -> JSONResponse:
    return JSONResponse({"status": "healthy", **instance_fingerprint()})


@app.post("/invocations")
async def invocations(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "payload must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload must be an object"}, status_code=400)

    try:
        user_id = resolve_user_id(
            request.headers.get(USER_ID_HEADER), payload.get("user_id")
        )
    except IsolationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    request_id = payload.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > REQUEST_ID_RE_MAX
    ):
        return JSONResponse(
            {"error": "payload.request_id must be a non-empty string"},
            status_code=400,
        )

    # The Session Router sends `warmup: true` on a fresh runtimeSessionId to
    # start the microVM and learn its boot fingerprint without running Claude.
    # It also checks that the shared workspace file system (S3 Files) is
    # mounted and stamps USERS_ROOT once, so the router can tell whether a new
    # environment sees the same workspace tree (marker present) or an empty /
    # wrong one (marker absent although warm-ups happened before).
    if payload.get("warmup") is True:
        mounted = os.path.ismount(WORKSPACE_MOUNT) if WORKSPACE_MOUNT else None
        if mounted is False:
            log.error("workspace mount %s is not mounted; refusing warm-up", WORKSPACE_MOUNT)
            return JSONResponse(
                {
                    "event": "error",
                    "warmup": True,
                    "request_id": request_id,
                    "message": f"workspace mount {WORKSPACE_MOUNT} is not mounted",
                    "storage_mounted": False,
                    "instance": instance_fingerprint(),
                },
                status_code=503,
            )
        ensure_workspace(USERS_ROOT, user_id)
        marker = USERS_ROOT / ".pool-marker"
        marker_present = marker.is_file()
        if not marker_present:
            try:
                marker.write_text(json.dumps({"first_warmup": STARTED_AT}) + "\n")
            except OSError as exc:
                log.warning("failed to write storage marker: %s", type(exc).__name__)
        return JSONResponse(
            {
                "event": "complete",
                "warmup": True,
                "request_id": request_id,
                "user_id": user_id,
                "users_root": str(USERS_ROOT),
                "storage_mounted": mounted,
                "storage_marker_present": marker_present,
                "instance": instance_fingerprint(),
            }
        )

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return JSONResponse({"error": "payload.prompt is required"}, status_code=400)
    reset = payload.get("reset", False)
    if not isinstance(reset, bool):
        return JSONResponse(
            {"error": "payload.reset must be a boolean"}, status_code=400
        )
    log.info(
        "accepted request request_id=%s prompt_chars=%d reset=%s inflight=%d",
        request_id,
        len(prompt),
        reset,
        _inflight,
    )

    async def stream():
        global _inflight
        lock = await _lock_for(user_id)
        async with lock:
            async with _agent_slots:
                _inflight += 1
                try:
                    async for chunk in _run_agent(user_id, prompt, reset, request_id):
                        yield chunk
                except Exception as exc:
                    log.exception("agent request failed")
                    yield _sse(
                        {
                            "event": "error",
                            "request_id": request_id,
                            "user_id": user_id,
                            "message": f"{type(exc).__name__}: {exc}",
                            "instance": instance_fingerprint(),
                        }
                    )
                finally:
                    _inflight -= 1

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    USERS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    uvicorn.run(app, host="0.0.0.0", port=8080)
