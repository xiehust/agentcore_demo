"""Offline checks of real spawn concurrency and failures; never contact AWS."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

import analyze
import client


class Events:
    def __init__(self):
        self.handlers = {}

    def register(self, name, fn):
        self.handlers[name] = fn

    def emit(self, name):
        self.handlers[name]()


class FakeClient:
    def __init__(self, pool, corrupt=False):
        self.meta = SimpleNamespace(events=Events(),
            config=SimpleNamespace(max_pool_connections=pool, retries={"total_max_attempts": 1}))
        self.counts = {}
        self.lock = threading.Lock()
        self.corrupt = corrupt

    def invoke_agent_runtime(self, **kwargs):
        operation = "bedrock-agentcore.InvokeAgentRuntime"
        self.meta.events.emit("before-send." + operation)
        start = time.time()
        time.sleep(.005)
        self.meta.events.emit("response-received." + operation)
        sid = kwargs["runtimeSessionId"]
        with self.lock:
            self.counts[sid] = self.counts.get(sid, 0) + 1
            seq = self.counts[sid]
        body = {"ok": True, "echo": json.loads(kwargs["payload"])["nonce"],
            "instance_id": "corrupt" if self.corrupt else sid, "request_index": seq,
            "handler_started_unix_s": start, "handler_finished_unix_s": time.time()}
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0},
                "response": io.BytesIO(json.dumps(body).encode())}

    def stop_runtime_session(self, **kwargs):
        operation = "bedrock-agentcore.StopRuntimeSession"
        self.meta.events.emit("before-send." + operation)
        self.meta.events.emit("response-received." + operation)
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}

    def close(self):
        pass


def factory(region, pool):
    return FakeClient(pool)


def corrupt_factory(region, pool):
    return FakeClient(pool, corrupt=True)


def broken_factory(region, pool):
    raise RuntimeError("intentional credential preparation failure")


class Tests(unittest.TestCase):
    def test_real_spawn_and_phase_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = client.run_burst("offline", "offline", 50, Path(directory), factory=factory)
            summary = analyze.summarize(raw)
            self.assertTrue(summary["all_checks_pass"], summary)
            self.assertEqual(summary["process_count"], 8)
            # The fake responds in 5ms; scheduling may finish a request before
            # the last thread runs. Full overlap is measured against live AWS.
            self.assertGreater(summary["client_peak"], 1)
            self.assertEqual(len([e for r in raw["reports"] for e in r["events"]]), 150)

    def test_single_request_uses_one_process(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = client.run_burst("offline", "offline", 1, Path(directory), factory=factory)
            self.assertTrue(analyze.summarize(raw)["all_checks_pass"])

    def test_duplicate_instance_marker_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = client.run_burst("offline", "offline", 10, Path(directory), factory=corrupt_factory)
            self.assertFalse(analyze.summarize(raw)["checks"]["unique_instances"])

    def test_worker_init_failure_does_not_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = client.run_burst("offline", "offline", 10, Path(directory),
                factory=broken_factory, ready_timeout=10, finish_timeout=10)
            self.assertTrue(raw["failure"])
            self.assertEqual(raw["release_perf"], 0)


if __name__ == "__main__":
    unittest.main()
