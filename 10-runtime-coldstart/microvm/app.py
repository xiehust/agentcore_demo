"""Ping-pong agent for the Lambda MicroVMs cold-start benchmark.

Same agent code path as app/main.py (the AgentCore variant): the real
`bedrock-agentcore` SDK server (BedrockAgentCoreApp) on 0.0.0.0:8080 serving
GET /ping and POST /invocations. On top of that, a tiny stdlib HTTP server on
0.0.0.0:9000 implements the Lambda MicroVMs lifecycle hooks
(POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}).

Timestamps returned by /invocations let the client decompose the cold start:

- proc_start_ts  : module import. Happens at IMAGE BUILD time — the process is
                   snapshotted after /ready and every MicroVM resumes from that
                   snapshot, so this value is shared by all VMs of an image.
- run_hook_ts    : wall clock when Lambda called /run on THIS MicroVM
                   (first thing after snapshot restore).
- resume_hook_ts : wall clock of the latest /resume (suspend -> resume cycle).
- request_ts     : arrival of the /invocations request.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bedrock_agentcore.runtime import BedrockAgentCoreApp

PROC_START_TS = time.time()
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
HOOK_PORT = 9000
AGENT_URL = "http://127.0.0.1:8080"
KNOWN_HOOKS = ("ready", "validate", "run", "resume", "suspend", "terminate")

STATE = {
    "run_hook_ts": None,
    "resume_hook_ts": None,
    "hook_calls": {},          # hook name -> count
    "microvm_id": None,
    "run_hook_payload": None,
}
_lock = threading.Lock()


def agent_answers(path: str, data: bytes | None = None) -> bool:
    """True when the agent server on :8080 handles `path` with HTTP 200."""
    req = urllib.request.Request(
        AGENT_URL + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


class HookHandler(BaseHTTPRequestHandler):
    """Lifecycle hooks. /ready and /validate gate on the real agent server
    (503 = "retry", per the hook contract); the runtime hooks answer 200 at
    once and only record when they fired."""

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        hook = self.path[len(HOOK_PREFIX):] if self.path.startswith(HOOK_PREFIX) else ""
        if hook not in KNOWN_HOOKS:
            self._reply(404, {"error": f"unknown hook path {self.path}"})
            return
        now = time.time()
        with _lock:
            STATE["hook_calls"][hook] = STATE["hook_calls"].get(hook, 0) + 1
            if hook == "run":
                STATE["run_hook_ts"] = now
                try:
                    body = json.loads(raw or b"{}")
                    STATE["microvm_id"] = body.get("microvmId")
                    STATE["run_hook_payload"] = body.get("runHookPayload")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            elif hook == "resume":
                STATE["resume_hook_ts"] = now
        status = 200
        if hook == "ready":
            # Snapshot only once uvicorn is serving — otherwise every MicroVM
            # would replay the tail of the boot sequence.
            status = 200 if agent_answers("/ping") else 503
        elif hook == "validate":
            # Exercise the hot path on the test VM so Lambda can sample (and
            # later prefetch) the snapshot pages a real /invocations touches.
            status = 200 if agent_answers("/invocations", b'{"ping": "validate"}') else 503
        self._reply(status, {"hook": hook, "ts": now})

    def do_GET(self):  # noqa: N802 — handy for local debugging
        with _lock:
            snapshot = dict(STATE)
        self._reply(200, snapshot)

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep hook traffic out of stdout
        return


def serve_hooks() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler)
    server.daemon_threads = True
    server.serve_forever()


app = BedrockAgentCoreApp()


@app.entrypoint
def handler(payload):
    with _lock:
        state = dict(STATE)
    return {
        "message": "pong",
        "origin": "lambda-microvm",
        "proc_start_ts": PROC_START_TS,
        "run_hook_ts": state["run_hook_ts"],
        "resume_hook_ts": state["resume_hook_ts"],
        "hook_calls": state["hook_calls"],
        "microvm_id": state["microvm_id"],
        "request_ts": time.time(),
        "echo": payload,
    }


if __name__ == "__main__":
    threading.Thread(target=serve_hooks, name="microvm-hooks", daemon=True).start()
    app.run()
