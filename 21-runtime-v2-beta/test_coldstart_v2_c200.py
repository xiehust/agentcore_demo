"""Offline c200 tests: concurrent mock calls, failures, and owned cleanup."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError, ReadTimeoutError
import coldstart_v2_c200 as c


def response(body=None):
    return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0},
            "response": io.BytesIO(json.dumps(body if body is not None else {
                "message": "pong", "proc_start_ts": 1, "request_ts": 2,
                "echo": {"ping": "coldstart"}}).encode())}


class C200Tests(unittest.TestCase):
    def exercise(self, invoke_effect, stop_error=None):
        with tempfile.TemporaryDirectory() as tmp:
            raw = MagicMock()
            raw.invoke_agent_runtime.side_effect = invoke_effect
            raw.stop_runtime_session.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
            raw.stop_runtime_session.side_effect = stop_error
            recorder = c.Recorder(raw, Path(tmp))
            with patch.object(c.bench, "timed_invoke", recorder.timed_invoke):
                result = c.bench.probe(recorder, "arn", "500mb", 200, 1, 0, None)
            return result, recorder.events, raw

    def test_success_records_cold_warm_stop(self):
        result, events, raw = self.exercise([response(), response()])
        self.assertTrue(result["success"] and result["stopped"])
        self.assertEqual([e["operation"] for e in events], ["invoke", "invoke", "stop"])
        self.assertEqual([e["attempt"] for e in events[:2]], [1, 2])
        self.assertEqual(raw.invoke_agent_runtime.call_count, 2)
        self.assertTrue(all(e["valid"] for e in events[:2]))

    def test_throttle_preserved_without_retry_or_warm(self):
        error = ClientError({"Error": {"Code": "ThrottlingException", "Message": "limited"},
                            "ResponseMetadata": {"HTTPStatusCode": 429, "RetryAttempts": 0}}, "InvokeAgentRuntime")
        result, events, raw = self.exercise(error)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_type"], "throttle")
        self.assertEqual(events[0]["error_code"], "ThrottlingException")
        raw.invoke_agent_runtime.assert_called_once()
        raw.stop_runtime_session.assert_called_once()

    def test_timeout_still_stops(self):
        result, events, raw = self.exercise(ReadTimeoutError(endpoint_url="https://example.invalid"))
        self.assertEqual(result["error_type"], "timeout")
        self.assertGreaterEqual(events[0]["elapsed_ms"], 0)
        raw.stop_runtime_session.assert_called_once()

    def test_malformed_body_not_success(self):
        result, events, raw = self.exercise([response({})])
        self.assertFalse(result["success"])
        self.assertEqual(events[0]["body"], {})
        raw.stop_runtime_session.assert_called_once()

    def test_warm_failure_keeps_cold_sample(self):
        result, events, raw = self.exercise([response(), RuntimeError("warm failed")])
        self.assertTrue(result["success"])
        self.assertIsNone(result["warm_ms"])
        self.assertIn("warm failed", result["error_msg"])
        self.assertEqual(events[1]["attempt"], 2)
        raw.stop_runtime_session.assert_called_once()

    def test_stop_failure_visible(self):
        result, events, raw = self.exercise([response(), response()], RuntimeError("stop failed"))
        self.assertTrue(result["success"])
        self.assertFalse(result["stopped"])
        self.assertEqual(events[-1]["operation"], "stop")
        self.assertIn("error", events[-1])

    def test_real_200_thread_mock_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            raw = MagicMock()
            raw.invoke_agent_runtime.side_effect = lambda **kwargs: response()
            raw.stop_runtime_session.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
            recorder = c.Recorder(raw, out)
            dep = {"region": "us-west-2", "runtimes": {"500mb": {"arn": "arn", "name": "test"}}}
            with patch.object(c.bench, "timed_invoke", recorder.timed_invoke):
                summary = c.run_burst(recorder, dep, "500mb", out)
            self.assertEqual(summary["samples"], 200)
            self.assertEqual(summary["success"], 200)
            self.assertEqual(summary["stop_success"], 200)
            self.assertEqual(raw.invoke_agent_runtime.call_count, 400)
            self.assertEqual(raw.stop_runtime_session.call_count, 200)
            cell = json.loads((out / "raw" / "500mb_c200.json").read_text())
            self.assertEqual(len({r["session_id"] for r in cell["requests"]}), 200)
            self.assertEqual({r["request_idx"] for r in cell["requests"]}, set(range(200)))

    def test_launch_failure_aborts_barrier_without_invokes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            dep = {"region": "us-west-2", "runtimes": {"500mb": {"arn": "arn", "name": "test"}}}
            with patch.object(c.threading.Thread, "start", side_effect=RuntimeError("thread limit")), patch.object(
                    c.bench, "probe") as probe:
                with self.assertRaises(RuntimeError):
                    c.run_burst(MagicMock(), dep, "500mb", out)
            probe.assert_not_called()
            self.assertEqual(json.loads((out / "raw" / "500mb_c200.json").read_text())["requests"], [])

    def test_partial_deployment_cleanup_only_targets_saved_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = MagicMock()
            ctl.get_agent_runtime.side_effect = ClientError({"Error": {
                "Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
            self.assertTrue(c.original.cleanup(ctl, {"runtimes": {"500mb": {"id": "owned-id"}}}, Path(tmp)))
            ctl.delete_agent_runtime.assert_called_once_with(agentRuntimeId="owned-id")



if __name__ == "__main__":
    unittest.main()
