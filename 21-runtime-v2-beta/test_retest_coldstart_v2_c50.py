"""Offline c50 retest checks; cloud clients are mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import retest_coldstart_v2_c50 as c
from test_coldstart_v2_c200 import response


class C50Tests(unittest.TestCase):
    def test_exact_50_threads_and_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            raw = MagicMock()
            raw.invoke_agent_runtime.side_effect = lambda **kwargs: response()
            raw.stop_runtime_session.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
            recorder = c.shared.Recorder(raw, out)
            with patch.object(c.shared.bench, "timed_invoke", recorder.timed_invoke):
                summary = c.run_burst(recorder, {"arn": "arn", "name": "test"}, out)
            self.assertEqual(summary["samples"], 50)
            self.assertEqual(summary["success"], 50)
            self.assertEqual(summary["warm_success"], 50)
            self.assertEqual(summary["stop_success"], 50)
            self.assertEqual(raw.invoke_agent_runtime.call_count, 100)
            self.assertEqual(raw.stop_runtime_session.call_count, 50)
            result = json.loads((out / "raw/500mb_c50.json").read_text())
            self.assertEqual(result["meta"]["concurrency"], 50)
            self.assertEqual({r["request_idx"] for r in result["requests"]}, set(range(50)))
            self.assertEqual(len({r["session_id"] for r in result["requests"]}), 50)

    def test_failed_thread_launch_does_not_invoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            with patch.object(c.threading.Thread, "start", side_effect=RuntimeError("limit")), patch.object(
                    c.shared.bench, "probe") as probe:
                with self.assertRaises(RuntimeError):
                    c.run_burst(MagicMock(), {"arn": "arn", "name": "test"}, out)
            probe.assert_not_called()
            self.assertEqual(json.loads((out / "raw/500mb_c50.json").read_text())["requests"], [])

    def test_one_runtime_mocked_lifecycle(self):
        from botocore.session import Session
        prior = c.HERE / "results/coldstart_v2_c100_500mb_2026-09-11"
        request = json.loads((prior / "create_requests.json").read_text())["500mb"]
        meta = json.loads((prior / "run.json").read_text())
        image = json.loads((prior / "images.json").read_text())["500mb"]
        control, ecr, sts, raw = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        control.meta.service_model = Session().get_service_model("bedrock-agentcore-control")
        control.create_agent_runtime.return_value = {"agentRuntimeId": "owned", "agentRuntimeArn": "arn"}
        ecr.describe_images.return_value = {"imageDetails": [image]}
        sts.get_caller_identity.return_value = {"Account": meta["account"]}
        session = MagicMock()
        session.client.side_effect = lambda service, **kw: {
            "sts": sts, "bedrock-agentcore-control": control, "ecr": ecr}[service]
        raw.meta.config.max_pool_connections = 100
        raw.meta.config.retries = {"total_max_attempts": 1}
        with tempfile.TemporaryDirectory() as tmp, patch.object(c.boto3, "Session", return_value=session), \
                patch.object(c.shared.bench, "make_client", return_value=raw) as make, \
                patch.object(c.shared, "read_quotas", return_value=[]), \
                patch.object(c.shared.original, "wait_ready", return_value={"platformVersion": "V2"}), \
                patch.object(c.shared.original, "wait_endpoint_ready"), \
                patch.object(c.shared.bench, "probe", return_value={"success": True, "warm_ms": 1, "stopped": True}), \
                patch.object(c, "run_burst", return_value={"success": 50, "warm_success": 50, "stop_success": 50}) as burst, \
                patch.object(c.time, "sleep"), \
                patch.object(c.shared.original, "cleanup", return_value=True) as cleanup:
            out = Path(tmp)
            self.assertEqual(c.run(out), 0)
            make.assert_called_once_with("us-west-2", 50)
            control.create_agent_runtime.assert_called_once()
            actual = control.create_agent_runtime.call_args.kwargs
            for key in ["agentRuntimeArtifact", "roleArn", "lifecycleConfiguration", "platformVersion"]:
                self.assertEqual(actual[key], request[key])
            burst.assert_called_once()
            self.assertEqual(set(cleanup.call_args.args[1]["runtimes"]), {"500mb"})
            self.assertTrue(json.loads((out / "run.json").read_text())["all_invocations_successful"])



if __name__ == "__main__":
    unittest.main()
