"""Shared-runtime-session multi-user agent server (AgentCore HTTP contract).

One AgentCore runtime session == one instance of this process. Multiple end
users share it concurrently; per-user isolation is enforced by:

- a per-user workspace under USERS_ROOT used as the Claude ``cwd``;
- a PreToolUse hook denying any tool call whose paths escape that workspace;
- per-user Claude session resume (conversation memory never crosses users);
- a per-user asyncio lock (same user serialized, different users parallel).

Endpoints (AgentCore runtime HTTP protocol):
- ``GET  /ping``         → health check
- ``POST /invocations``  → SSE stream of agent events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from isolation import (  # noqa: E402
    IsolationError,
    ensure_workspace,
    guard_tool_call,
    validate_user_id,
)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shared-runtime")

USERS_ROOT = Path(os.environ.get("USERS_ROOT", "/mnt/scratch/users"))
MODEL = os.environ.get(
    "ANTHROPIC_MODEL", "us.anthropic.claude-sonnet-4-6"
)
MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
# Tune the subprocess cap for the selected instance size and workload.
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "4"))
USER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"

SERVER_RUN_ID = uuid.uuid4().hex  # proves "same container process"
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "LS", "TodoWrite"]
DISALLOWED_TOOLS = ["Bash", "WebFetch", "WebSearch", "Task", "KillBash"]

SYSTEM_PROMPT = """You are a per-user workspace assistant on shared infrastructure.

Rules:
- Operate ONLY inside your current working directory. Never mention, guess,
  or try to access any path outside it.
- Use relative paths for all file operations.
- Be concise: answer in at most three short sentences unless writing files.
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
    }


async def _lock_for(user_id: str) -> asyncio.Lock:
    async with _locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = asyncio.Lock()
        return _user_locks[user_id]


def _session_meta_path(workspace: Path) -> Path:
    return workspace / ".session_meta.json"


def _load_prev_session(workspace: Path) -> str | None:
    try:
        meta = json.loads(_session_meta_path(workspace).read_text())
        value = meta.get("claude_session_id")
        return value if isinstance(value, str) and value else None
    except (OSError, json.JSONDecodeError):
        return None


def _store_session(workspace: Path, session_id: str | None) -> None:
    if not session_id:
        return
    try:
        _session_meta_path(workspace).write_text(
            json.dumps({"claude_session_id": session_id})
        )
    except OSError as exc:
        log.warning("failed to persist session meta: %s", exc)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_options(workspace: Path, resume: str | None, denials: list[str]) -> ClaudeAgentOptions:
    async def path_guard(input_data, _tool_use_id, _context):
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        reason = guard_tool_call(tool_name, tool_input, workspace)
        if reason is None:
            return {}
        denials.append(f"{tool_name}: {reason}")
        log.warning("DENIED %s in %s: %s", tool_name, workspace.name, reason)
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
        setting_sources=[],  # never read host/project Claude settings
        resume=resume,
        hooks={
            "PreToolUse": [HookMatcher(matcher=None, hooks=[path_guard])]
        },
        env={
            # Per-user HOME keeps Claude CLI transcripts/config per workspace.
            "HOME": str(workspace),
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": os.environ.get("AWS_REGION", "us-west-2"),
            "AWS_DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
        },
    )


async def _run_agent(user_id: str, prompt: str, reset: bool):
    """Async generator yielding SSE strings for one user request."""
    workspace = ensure_workspace(USERS_ROOT, user_id)
    resume = None if reset else _load_prev_session(workspace)
    denials: list[str] = []
    options = _build_options(workspace, resume, denials)

    result_text = None
    new_session_id = None
    is_error = False

    async for message in query(prompt=prompt, options=options):
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
            "result": result_text,
            "is_error": is_error,
            "user_id": user_id,
            "workspace": str(workspace),
            "claude_session_id": new_session_id,
            "resumed_from": resume,
            "denied_count": len(denials),
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

    raw_user = request.headers.get(USER_ID_HEADER) or payload.get("user_id")
    try:
        user_id = validate_user_id(raw_user)
    except IsolationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return JSONResponse({"error": "payload.prompt is required"}, status_code=400)
    reset = bool(payload.get("reset", False))

    log.info("request user=%s prompt=%.60r", user_id, prompt)

    async def stream():
        lock = await _lock_for(user_id)
        async with lock:  # same user serialized; different users run in parallel
            async with _agent_slots:  # cap total Claude subprocesses
                try:
                    async for chunk in _run_agent(user_id, prompt, reset):
                        yield chunk
                except Exception as exc:  # surface errors into the SSE stream
                    log.exception("agent failure for user %s", user_id)
                    yield _sse(
                        {
                            "event": "error",
                            "user_id": user_id,
                            "message": f"{type(exc).__name__}: {exc}",
                            "instance": instance_fingerprint(),
                        }
                    )

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    USERS_ROOT.mkdir(parents=True, exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8080)
