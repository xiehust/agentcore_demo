"""Lambda MicroVMs entrypoint: the probed agent from app/main.py plus lifecycle hooks.

Lambda builds the image, starts this process, waits for /ready to return 200,
then snapshots memory + disk; every RunMicrovm resumes from that snapshot.

AGENT_START keeps its app/main.py meaning:
  lazy  - Claude CLI starts on the first /invocations after the MicroVM starts
  eager - CLI starts when the server listens; /ready answers 503 until it is
          connected, so the snapshot always contains a ready CLI
VALIDATE_AGENT=1 also starts the CLI inside /validate. Lambda runs /validate on
a MicroVM started from the finished snapshot and records which snapshot regions
it touches, to prefetch them on later starts; that CLI is not in the snapshot.

Hooks listen on :9000 and record their wall-clock times in main.STATE, which
the agent already returns in every ``done`` event.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main  # main.py's module-level boot probes run here, i.e. at image build time
import uvicorn

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
HOOK_PORT = 9000
AGENT_URL = "http://127.0.0.1:8080"
VALIDATE_AGENT = os.environ.get("VALIDATE_AGENT") == "1"

os.makedirs(os.environ.get("HOME", "/tmp/agent-home"), exist_ok=True)
# Names only, never values: shows how the platform hands credentials to the process.
main.BOOT["aws_env_names"] = sorted(k for k in os.environ if k.startswith("AWS_"))
# AWS_REGION is a reserved image variable; Claude Code's Bedrock client needs it.
os.environ.setdefault("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
main.STATE.update(validate_hook_ns=None, run_hook_ns=None, microvm_id=None, hook_calls={})


@main.app.post("/_agent_start")
async def agent_start() -> dict:
    """Start the CLI from the hook thread via the server's own event loop."""
    await main.AGENT.client("validate")
    return {"connected": True}


def _agent_answers(path: str, body: bytes | None = None, timeout: float = 5) -> bool:
    req = urllib.request.Request(AGENT_URL + path, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


_validate_kick = threading.Event()


class HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        now = time.time_ns()
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        hook = self.path.removeprefix(HOOK_PREFIX)
        calls = main.STATE["hook_calls"]
        calls[hook] = calls.get(hook, 0) + 1
        status = 200
        if hook == "ready":
            # 503 must come back at once (holding the request open fails the build).
            ready = main.STATE["listening_ns"] is not None and _agent_answers("/ping")
            if main.AGENT_START == "eager":
                ready = ready and bool(main.AGENT.probes)
            status = 200 if ready else 503
        elif hook == "validate":
            main.STATE["validate_hook_ns"] = main.STATE["validate_hook_ns"] or now
            if VALIDATE_AGENT:
                if not _validate_kick.is_set():
                    _validate_kick.set()
                    threading.Thread(target=_agent_answers, args=("/_agent_start", b"", 300), daemon=True).start()
                status = 200 if main.AGENT.probes else 503
            else:
                status = 200 if _agent_answers("/ping") else 503
        elif hook == "run":
            main.STATE["run_hook_ns"] = now
            try:
                main.STATE["microvm_id"] = json.loads(raw or b"{}").get("microvmId")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        elif hook not in ("resume", "suspend", "terminate"):
            status = 404
        body = json.dumps({"hook": hook, "ts_ns": now}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - keep hook polling out of the logs
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), HookHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="microvm-hooks", daemon=True).start()
    config = uvicorn.Config(main.app, host="0.0.0.0", port=8080, log_level="warning", access_log=False)
    main.ProbedServer(config).run()
