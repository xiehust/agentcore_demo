"""Minimal dependency-free HTTP service for infrastructure latency measurements."""
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROCESS_STARTED_UNIX = time.time()
PROCESS_STARTED_MONO = time.monotonic()
PROCESS_ID = str(uuid.uuid4())
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
COUNTER = 0
INSTANCE_ID = None
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send_json(self, status, data):
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        self.send_json(200 if self.path == "/ping" else 404,
                       {"status": "Healthy"} if self.path == "/ping" else {"error": "not found"})

    def do_POST(self):
        global COUNTER, INSTANCE_ID
        received_unix = time.time()
        received_mono = time.monotonic()
        if self.path != "/invocations":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("payload length out of range")
            payload = json.loads(self.rfile.read(length))
            hold = float(payload.get("hold_seconds", 0))
            if not 0 <= hold <= 5:
                raise ValueError("hold_seconds out of range")
        except (ValueError, TypeError, AttributeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        with LOCK:
            # Import-time IDs can be captured in a platform snapshot and cloned.
            # Create the identity after restore, on the first actual invocation.
            if INSTANCE_ID is None:
                INSTANCE_ID = str(uuid.uuid4())
            COUNTER += 1
            sequence = COUNTER
        if hold:
            time.sleep(hold)
        self.send_json(200, {
            "ok": True, "echo": payload.get("nonce"), "process_id": PROCESS_ID,
            "instance_id": INSTANCE_ID,
            "boot_id": BOOT_ID, "pid": os.getpid(), "request_index": sequence,
            "process_started_unix_s": PROCESS_STARTED_UNIX,
            "process_age_at_request_ms": (received_mono - PROCESS_STARTED_MONO) * 1000,
            "handler_started_unix_s": received_unix,
            "handler_finished_unix_s": time.time(),
            "handler_ms": (time.monotonic() - received_mono) * 1000,
        })

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(json.dumps({"event": "started", "process_id": PROCESS_ID,
                      "boot_id": BOOT_ID, "at": PROCESS_STARTED_UNIX}), flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
