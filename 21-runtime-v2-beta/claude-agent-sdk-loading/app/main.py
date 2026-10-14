"""Claude Agent SDK agent for AgentCore Runtime V2, instrumented with latency probes.

Every probe is a wall-clock nanosecond timestamp taken on this microVM, so the
client can place them on the same time axis as its own send/receive times
(both sides sync to the Amazon Time Sync Service; bench.py checks ordering).
Probes are returned in the final SSE ``done`` event and logged as JSON lines.

AGENT_START selects when the Claude Code CLI subprocess is started:
  lazy  - on the first /invocations of the microVM; startup is in that request
  eager - in the background as soon as the HTTP server listens
The client is kept for the life of the microVM (one AgentCore session), so later
requests in the same session are further turns of one warm conversation.
"""

import time

T_MODULE_NS = time.time_ns()  # first statement: before any non-trivial import

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
from pathlib import Path  # noqa: E402

T_STDLIB_NS = time.time_ns()

import claude_agent_sdk  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
)
from claude_agent_sdk._cli_version import __cli_version__  # noqa: E402

T_SDK_IMPORT_NS = time.time_ns()

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

T_IMPORTS_NS = time.time_ns()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-v2-probe")

AGENT_START = os.environ.get("AGENT_START", "lazy")
MODEL = os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-5-5")
WORKDIR = Path(os.environ.get("AGENT_WORKDIR", "/tmp/agent-work"))
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
SYSTEM_PROMPT = "You are a concise assistant. Answer in one short sentence."
BUNDLED_CLI = Path(claude_agent_sdk.__path__[0]) / "_bundled" / "claude"


def _proc_start_s(pid: str) -> float:
    """Process start time in seconds after kernel boot (/proc/<pid>/stat field 22)."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")


def _vm_counters() -> dict:
    """Page cache size and paging counters: show when a step is waiting on disk reads."""
    out: dict = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("Cached:", "MemFree:")):
                out[line.split(":")[0].lower() + "_kb"] = int(line.split()[1])
        for line in Path("/proc/vmstat").read_text().splitlines():
            key, value = line.split()
            if key in ("pgmajfault", "pgpgin"):
                out[key] = int(value)
    except OSError:
        pass
    return out


def _wall_minus_mono_ns() -> int:
    """Grows only while the guest is not running (paused, snapshotted, restored later)."""
    return time.time_ns() - time.monotonic_ns()


def _boot_probes() -> dict:
    """Where this process sits on the microVM's timeline.

    kernel_boot_ns is wall-now minus /proc/uptime, so it is the guest kernel's
    effective boot time (10 ms resolution). PID 1 and this process's start
    times are derived from it with clock-tick resolution.
    """
    now_ns = time.time_ns()
    uptime_s = float(Path("/proc/uptime").read_text().split()[0])
    kernel_boot_ns = now_ns - int(uptime_s * 1e9)
    btime = next(
        int(line.split()[1])
        for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("btime")
    )
    try:
        pid1_cmd = Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    except OSError:
        pid1_cmd = None
    return {
        "kernel_boot_ns": kernel_boot_ns,
        "pid1_start_ns": kernel_boot_ns + int(_proc_start_s("1") * 1e9),
        "proc_start_ns": kernel_boot_ns + int(_proc_start_s("self") * 1e9),
        "module_ns": T_MODULE_NS,
        "stdlib_ns": T_STDLIB_NS,
        "sdk_import_ns": T_SDK_IMPORT_NS,
        "imports_ns": T_IMPORTS_NS,
        "uptime_at_import_s": uptime_s,
        "btime_s": btime,
        "pid1_cmdline": pid1_cmd,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "cpu_count": os.cpu_count(),
        "wall_minus_mono_ns": _wall_minus_mono_ns(),
        "vm": _vm_counters(),
    }


BOOT = _boot_probes()
STATE = {"listening_ns": None, "first_ping_ns": None, "ping_count": 0, "requests": 0}


def _emit(kind: str, **fields) -> None:
    print(json.dumps({"probe": kind, **fields}, default=str), flush=True)


def _options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        setting_sources=[],
        include_partial_messages=True,
        cwd=str(WORKDIR),
        env={
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
            "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
        },
        stderr=lambda line: log.warning("claude-cli: %s", line.rstrip()),
    )


class Agent:
    """One ClaudeSDKClient (= one Claude Code CLI process) per microVM."""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.probes: dict = {}
        self.turn_lock = asyncio.Lock()

    def start(self, trigger: str) -> asyncio.Task:
        failed = self.task is not None and self.task.done() and (
            self.task.cancelled() or self.task.exception() is not None
        )
        if self.task is None or failed:
            self.task = asyncio.create_task(self._connect(trigger))
        return self.task

    async def _connect(self, trigger: str) -> ClaudeSDKClient:
        WORKDIR.mkdir(parents=True, exist_ok=True)
        probes = {
            "trigger": trigger,
            "connect_start_ns": time.time_ns(),
            "vm_before": _vm_counters(),
        }
        client = ClaudeSDKClient(options=_options())
        await client.connect()  # spawn CLI + initialize control handshake
        probes["connect_done_ns"] = time.time_ns()
        probes["vm_after"] = _vm_counters()
        probes["connect_ms"] = (probes["connect_done_ns"] - probes["connect_start_ns"]) / 1e6
        self.probes = probes
        _emit("agent_ready", **probes)
        return client

    async def client(self, trigger: str) -> ClaudeSDKClient:
        return await asyncio.shield(self.start(trigger))


AGENT = Agent()
app = FastAPI()


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/ping")
async def ping() -> JSONResponse:
    if STATE["first_ping_ns"] is None:
        STATE["first_ping_ns"] = time.time_ns()
    STATE["ping_count"] += 1
    return JSONResponse({"status": "Healthy", "time_of_last_update": int(time.time())})


@app.post("/invocations")
async def invocations(request: Request):
    received_ns = time.time_ns()
    STATE["requests"] += 1
    seq = STATE["requests"]
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "payload must be JSON"}, status_code=400)
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return JSONResponse({"error": "payload.prompt is required"}, status_code=400)
    session_id = request.headers.get(SESSION_HEADER)
    probes = {"request_received_ns": received_ns, "payload_parsed_ns": time.time_ns(),
              "wall_minus_mono_ns": _wall_minus_mono_ns(), "vm": _vm_counters()}

    async def stream():
        # Flushed before any agent work: the client's first byte measures delivery.
        yield _sse({"event": "accepted", "seq": seq, "request_received_ns": received_ns})
        result: dict = {}
        error = None
        text: list[str] = []
        try:
            async with AGENT.turn_lock:
                probes["turn_lock_ns"] = time.time_ns()
                client = await AGENT.client("request")
                probes["agent_ready_ns"] = time.time_ns()
                await client.query(prompt)
                probes["query_sent_ns"] = time.time_ns()
                async for msg in client.receive_response():
                    now = time.time_ns()
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        probes.setdefault("cli_init_msg_ns", now)
                    elif isinstance(msg, StreamEvent):
                        event = msg.event
                        if event.get("type") == "message_start":
                            probes.setdefault("message_start_ns", now)
                        elif (
                            event.get("type") == "content_block_delta"
                            and event.get("delta", {}).get("type") == "text_delta"
                        ):
                            probes.setdefault("first_token_ns", now)
                            text.append(event["delta"]["text"])
                            yield _sse({"event": "delta", "text": event["delta"]["text"]})
                    elif isinstance(msg, AssistantMessage):
                        probes.setdefault("assistant_message_ns", now)
                    elif isinstance(msg, ResultMessage):
                        probes["result_ns"] = now
                        result = {
                            "is_error": msg.is_error,
                            "subtype": msg.subtype,
                            "duration_ms": msg.duration_ms,
                            "duration_api_ms": msg.duration_api_ms,
                            "num_turns": msg.num_turns,
                            "usage": msg.usage,
                            "total_cost_usd": msg.total_cost_usd,
                            "claude_session_id": msg.session_id,
                        }
        except Exception as exc:  # reported to the client, not swallowed
            log.exception("agent turn failed")
            error = f"{type(exc).__name__}: {exc}"
        probes["done_sent_ns"] = time.time_ns()
        done = {
            "event": "done",
            "seq": seq,
            "session_id": session_id,
            "agent_start": AGENT_START,
            "model": MODEL,
            "text": "".join(text),
            "error": error,
            "result": result,
            "probes": probes,
            "agent": AGENT.probes,
            "boot": BOOT,
            "server": {**STATE},
            "versions": {"claude_agent_sdk": claude_agent_sdk.__version__, "claude_cli": __cli_version__},
        }
        _emit("request", **{k: v for k, v in done.items() if k not in ("event", "boot")})
        yield _sse(done)

    return StreamingResponse(stream(), media_type="text/event-stream")


class ProbedServer(uvicorn.Server):
    """Records the moment the socket accepts connections, then (eager) starts the agent."""

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        STATE["listening_ns"] = time.time_ns()
        _emit("boot", agent_start=AGENT_START, cli_bytes=BUNDLED_CLI.stat().st_size,
              **BOOT, listening_ns=STATE["listening_ns"])
        if AGENT_START == "eager":
            AGENT.start("boot")


if __name__ == "__main__":
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning", access_log=False)
    ProbedServer(config).run()
