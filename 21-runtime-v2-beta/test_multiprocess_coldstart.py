"""Real-spawn offline checks with fake transport; no AWS calls."""
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from botocore.exceptions import ClientError, ReadTimeoutError
import multiprocess_coldstart_client as client

class FakeEvents:
    def __init__(self):
        self.handlers = {}

    def register(self, name, handler):
        self.handlers[name] = handler

    def emit(self, name):
        self.handlers[name]()


class FakeClient:
    def __init__(self, mode="success"):
        self.meta = SimpleNamespace(events=FakeEvents(), config=SimpleNamespace(
            max_pool_connections=0, retries={"total_max_attempts": 1}))
        self.mode = mode

    def invoke_agent_runtime(self, **kwargs):
        operation = "bedrock-agentcore.InvokeAgentRuntime"
        if self.mode == "presend":
            raise ReadTimeoutError(endpoint_url="https://invalid.example")
        self.meta.events.emit("before-send." + operation)
        if self.mode == "hang":
            time.sleep(10)
        time.sleep(.002)
        self.meta.events.emit("response-received." + operation)
        if self.mode == "throttle":
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "limited"},
                               "ResponseMetadata": {"HTTPStatusCode": 429, "RetryAttempts": 0}}, "InvokeAgentRuntime")
        body = {"message": "pong", "echo": {"ping": "coldstart"}, "proc_start_ts": 1, "request_ts": 2}
        stream = BrokenBody() if self.mode == "body" else io.BytesIO(json.dumps(body).encode())
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}, "response": stream}

    def stop_runtime_session(self, **kwargs):
        operation = "bedrock-agentcore.StopRuntimeSession"
        self.meta.events.emit("before-send." + operation)
        self.meta.events.emit("response-received." + operation)
        return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}

    def close(self):
        pass


class BrokenBody:
    def read(self):
        raise ReadTimeoutError(endpoint_url="https://invalid.example")

    def close(self):
        pass


def fake_factory(region, pool):
    raw = FakeClient()
    raw.meta.config.max_pool_connections = pool
    return raw


def failed_factory(region, pool):
    raise RuntimeError("initialization failed")


def crashed_factory(region, pool):
    os._exit(9)


def hung_factory(region, pool):
    return FakeClient("hang")


class MultiprocessTests(unittest.TestCase):
    def test_real_spawn_all_levels(self):
        for concurrency in (50, 100, 200):
            with self.subTest(concurrency=concurrency), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                summary = client.run_burst("us-west-2", "arn", concurrency, out, factory=fake_factory)
                self.assertEqual(summary["samples"], concurrency)
                self.assertEqual(summary["success"], concurrency)
                self.assertEqual(summary["warm_success"], concurrency)
                self.assertEqual(summary["stop_success"], concurrency)
                result = json.loads((out / "client_result.json").read_text())
                self.assertEqual(len({r["pid"] for r in result["reports"]}), 8)
                self.assertNotIn(os.getpid(), {r["pid"] for r in result["reports"]})
                self.assertEqual(sum(r["pool_size"] for r in result["reports"]), 2 * concurrency)
                events = [e for r in result["reports"] for e in r["events"]]
                self.assertEqual(len(events), 3 * concurrency)
                self.assertTrue(all(e["started_perf"] >= result["release_perf"] for e in events))
                self.assertTrue(all(len(e["before_send_perf"]) == len(e["response_received_perf"]) == 1 for e in events))
                self.assertEqual(result["exitcodes"], [0] * 8)

    def test_initialization_failure_is_bounded_and_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                client.run_burst("region", "arn", 2, Path(tmp), process_count=2,
                                 factory=failed_factory, ready_timeout=5, finish_timeout=5)
            result = json.loads((Path(tmp) / "client_result.json").read_text())
            self.assertIsNotNone(result["failure"])
            self.assertEqual(result["release_perf"], 0)

    def test_hung_worker_is_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TimeoutError):
                client.run_burst("region", "arn", 2, Path(tmp), process_count=2,
                                 factory=hung_factory, ready_timeout=10, finish_timeout=.5)
            result = json.loads((Path(tmp) / "client_result.json").read_text())
            self.assertTrue(all(code is not None for code in result["exitcodes"]))

    def test_crashed_worker_is_reaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises((TimeoutError, client.queue.Empty)):
                client.run_burst("region", "arn", 2, Path(tmp), process_count=2,
                                 factory=crashed_factory, ready_timeout=2, finish_timeout=2)
            result = json.loads((Path(tmp) / "client_result.json").read_text())
            self.assertTrue(all(code is not None for code in result["exitcodes"]))

    def test_hooks_keep_failure_timing_without_credentials(self):
        for mode in ("success", "throttle", "presend", "body"):
            with self.subTest(mode=mode):
                recorder = client.Recorder(FakeClient(mode))
                event = recorder.call("arn", "session", "invoke")
                self.assertEqual(bool(event.get("valid")), mode == "success")
                self.assertGreaterEqual(event["elapsed_ms"], 0)
                self.assertEqual(len(event["before_send_perf"]), 0 if mode == "presend" else 1)
                self.assertIsNone(recorder.local.event)
                self.assertNotIn("headers", event)
                self.assertIsNone(recorder.before_send())
                self.assertIsNone(recorder.response_received())



if __name__ == "__main__":
    unittest.main()
