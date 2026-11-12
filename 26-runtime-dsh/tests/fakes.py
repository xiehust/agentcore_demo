"""Offline stand-ins for AgentCore Runtime and the dsh wrapper. No AWS calls.

FakeRuntime implements the two client methods the benchmark uses and answers
with the wrapper's JSON contract. Timestamps come from a "microVM clock" that
is deliberately skewed from the client clock, so a test that passes proves the
boot-kind classification does not depend on the two clocks agreeing.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

DEPLOYMENTS = {
    "region": "us-east-1",
    "account": "111122223333",
    "model_id": "deepseek.test-model",
    "dsh_version": "9.9.9",
    "runtimes": {
        "dsh": {
            "name": "dsh_base", "arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/dsh_base-AAAA",
            "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/dsh:test",
            "docker_size_bytes": 1105337345, "ecr_size_bytes": 293053545,
            "env": {"DSH_PREWARM": "0"},
        },
        "dsh_prewarm": {
            "name": "dsh_prewarm", "arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/dsh_prewarm-BBBB",
            "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/dsh:test",
            "docker_size_bytes": 1105337345, "ecr_size_bytes": 293053545,
            "env": {"DSH_PREWARM": "1", "EXAMPLE_API_KEY": "must-not-be-printed"},
        },
    },
}


def _runtime(name: str, suffix: str, version: str, prewarm: str) -> dict:
    return {"name": name, "arn": f"arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/{name}-{suffix}",
            "image_uri": "111122223333.dkr.ecr.us-east-1.amazonaws.com/dsh:test",
            "docker_size_bytes": 1105337345, "ecr_size_bytes": 293053545,
            "platform_version": version, "env": {"DSH_PREWARM": prewarm}}


# Two variants on each platform version; deployed 10 minutes before FakeClock's start.
PLATFORM_DEPLOYMENTS = {
    "region": "us-east-1", "account": "111122223333", "model_id": "deepseek.test-model",
    "dsh_version": "9.9.9", "deployed_at": "2027-01-15T08:06:40+00:00",
    "runtimes": {
        "dsh": _runtime("dsh_base", "AAAA", "V1", "0"),
        "dsh_prewarm": _runtime("dsh_prewarm", "BBBB", "V1", "1"),
        "dsh_v2": _runtime("dsh_base_v2", "CCCC", "V2", "0"),
        "dsh_prewarm_v2": _runtime("dsh_prewarm_v2", "DDDD", "V2", "1"),
    },
}


def write_deployments(directory: Path, deployments: dict = DEPLOYMENTS) -> Path:
    path = Path(directory) / "deployments.json"
    path.write_text(json.dumps(deployments))
    return path


class FakeClock:
    """Replaces the `time` module inside coldstart_test for deterministic timings."""

    def __init__(self):
        self.now = 1000.0

    def perf_counter(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def client_error(code: str, status: int, operation: str = "InvokeAgentRuntime") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "simulated"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FakeEvents:
    """The slice of botocore's event system the benchmark client uses."""

    def __init__(self):
        self.handlers: dict[str, dict] = {}

    def register(self, name, handler, unique_id=None):
        self.handlers.setdefault(name, {})[unique_id or id(handler)] = handler

    def emit(self, name):
        for handler in self.handlers.get(name, {}).values():
            handler(request=None)


class Body:
    def __init__(self, data: bytes, advance):
        self.data, self.advance = data, advance

    def read(self) -> bytes:
        self.advance(0.005)
        return self.data


class FakeRuntime:
    """plan[i] describes the i-th new session:
        boot: "real" (default) | "pool"     pool_age: seconds the pool instance has been up
              "snapshot"                    restored from the runtime's one snapshot (platform
                                            V2): every session reports the same process start
        cold_dsh_boot: seconds from dsh spawn to its first output on the cold turn (default 1.2)
        fail: "throttle" | "http500" | "timeout"   the cold invoke raises
              "bad_body"                           the cold invoke returns non-JSON
              "agent_fail"                         the cold agent turn ends with ok=false
        warm_fail: "http500"                       the warm invoke raises
        warm_new_process: True                     the warm invoke lands on another process
        stop_fail: True                            StopRuntimeSession raises
        connect: seconds                           the cold invoke opens a new connection
        pin: {...}                                 what the container's page pinner reported

    `default` is the behavior of sessions beyond the plan. `client_delay` is the
    time every invoke spends in the client before botocore's before-send event.

    With a FakeClock every duration is exact. Without one the fake sleeps for
    real, scaled down, so threaded runs still overlap like real requests.
    """

    SKEW = 37.5  # microVM clock runs this many seconds ahead of the client clock

    def __init__(self, clock: FakeClock | None = None, plan: list[dict] | None = None,
                 scale: float = 0.002, default: dict | None = None, client_delay: float = 0.0):
        self.clock = clock
        self.scale = 1.0 if clock else scale
        self.plan = plan or []
        self.default = default or {}
        self.client_delay = client_delay
        self.crash_file = None  # set by fake_factory: exit the process on the first invoke
        self.meta = SimpleNamespace(events=FakeEvents())
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.snapshots: dict[str, float] = {}  # runtime ARN -> process start of its snapshot
        self.invokes: list[dict] = []
        self.stops: list[dict] = []
        self.inflight = 0
        self.max_inflight = 0

    def advance(self, seconds: float) -> None:
        if self.clock:
            self.clock.sleep(seconds)
        else:
            time.sleep(seconds * self.scale)

    def vm_now(self) -> float:
        return (self.clock.time() if self.clock else time.time()) + self.SKEW

    def invoke_agent_runtime(self, **kwargs):
        with self.lock:
            self.invokes.append(kwargs)
            session_id = kwargs["runtimeSessionId"]
            new = session_id not in self.sessions
            if new:
                index = len(self.sessions)
                behavior = self.plan[index] if index < len(self.plan) else self.default
                self.sessions[session_id] = {"behavior": behavior, "seq": 0, "proc_start": None}
            session = self.sessions[session_id]
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            return self._serve(session, new, json.loads(kwargs["payload"]), kwargs["agentRuntimeArn"])
        finally:
            with self.lock:
                self.inflight -= 1

    def _serve(self, session: dict, new: bool, payload: dict, arn: str):
        import coldstart_test
        if self.crash_file and not Path(self.crash_file).exists():
            Path(self.crash_file).write_text("crashed once")
            os._exit(17)  # the worker process dies mid-request, without reporting
        behavior = session["behavior"]
        fail = behavior.get("fail") if new else behavior.get("warm_fail")
        self.advance(self.client_delay)      # serialization + signing
        self.meta.events.emit(coldstart_test.BEFORE_SEND)
        if new and behavior.get("connect"):  # TCP + TLS setup of a new connection
            self.advance(behavior["connect"])
            coldstart_test._note_connect(behavior["connect"] * self.scale)
        if fail == "throttle":
            self.advance(0.05)
            raise client_error("ThrottlingException", 429)
        if fail == "http500":
            self.advance(0.05)
            raise client_error("RuntimeClientError", 500)
        if fail == "timeout":
            self.advance(0.05)
            raise ReadTimeoutError(endpoint_url="https://invalid.example")
        if new and behavior.get("boot") == "snapshot":
            with self.lock:
                self.snapshots.setdefault(arn, self.vm_now() - 300.0 * self.scale)
            session["proc_start"] = self.snapshots[arn]
            self.advance(1.9)
        elif new and behavior.get("boot", "real") == "pool":
            session["proc_start"] = self.vm_now() - behavior.get("pool_age", 120.0) * self.scale
            self.advance(0.15)
        elif new:
            self.advance(5.5)                       # image pull + microVM boot
            session["proc_start"] = self.vm_now()   # wrapper process starts
            self.advance(0.5)                       # wrapper up, request delivered
        else:
            if behavior.get("warm_new_process"):
                session["proc_start"] = self.vm_now()
                session["seq"] = 0
            self.advance(0.03)
        if fail == "bad_body":
            return self._response(b"<html>upstream error</html>")

        request_ts = self.vm_now()
        session["seq"] += 1
        body = {"mode": payload["mode"], "proc_start_ts": session["proc_start"],
                "request_ts": request_ts, "invocation_seq": session["seq"],
                "image_tag": "dsh-test", "dsh_version": "9.9.9",
                "prewarm": ({"enabled": True, "level": 3, "detail": behavior["pin"]}
                            if behavior.get("pin") else {"enabled": False})}
        if payload["mode"] == "agent":
            cold = session["seq"] == 1
            spawn = request_ts + 0.01 * self.scale
            ok = fail != "agent_fail"
            boot = behavior.get("cold_dsh_boot", 1.2) if cold else 0.4
            text, total = boot + (0.8 if cold else 0.6), boot + (1.8 if cold else 1.1)
            body["agent"] = {
                "ok": ok, "exit_code": 0 if ok else 1, "spawn_ts": spawn,
                "first_output_ts": spawn + boot * self.scale,
                "first_text_ts": spawn + text * self.scale,
                "exit_ts": spawn + total * self.scale,
                "model": "deepseek.test-model",
                "output": "pong" if ok else "", "error": None if ok else "model access | denied",
            }
            self.advance(0.01 + total + 0.005)
        else:
            self.advance(0.002)
        body["response_ts"] = self.vm_now()
        return self._response(json.dumps(body).encode())

    def _response(self, data: bytes) -> dict:
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": f"req-{len(self.invokes)}"},
                "statusCode": 200, "contentType": "application/json",
                "response": Body(data, self.advance)}

    def stop_runtime_session(self, **kwargs):
        with self.lock:
            self.stops.append(kwargs)
            session = self.sessions.get(kwargs["runtimeSessionId"], {})
        if session.get("behavior", {}).get("stop_fail"):
            raise client_error("ResourceNotFoundException", 404, "StopRuntimeSession")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "statusCode": 200}


def fake_factory(region, pool_size, read_timeout, worker=None, *, default=None, scale=0.002,
                 client_delay=0.0, fail_worker=None, hang_worker=None, crash_worker=None,
                 crash_once_file=None):
    """Picklable client factory for spawned worker processes (use functools.partial).
    fail_worker: that worker cannot build its client. hang_worker: that worker never
    finishes building it. crash_worker: that worker's process exits on its first
    invoke, once (crash_once_file remembers it happened)."""
    if worker is not None and worker == fail_worker:
        raise RuntimeError("simulated client construction failure")
    if worker is not None and worker == hang_worker:
        time.sleep(600)
    fake = FakeRuntime(scale=scale, default=default, client_delay=client_delay)
    if worker is not None and worker == crash_worker:
        fake.crash_file = crash_once_file
    return fake
