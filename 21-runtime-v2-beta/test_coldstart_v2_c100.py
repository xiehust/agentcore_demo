"""Offline c100 orchestration tests; no AWS requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import coldstart_v2_c100 as c
from test_coldstart_v2_c200 import response


class C100Tests(unittest.TestCase):
    def test_exact_100_thread_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            raw = MagicMock()
            raw.invoke_agent_runtime.side_effect = lambda **kwargs: response()
            raw.stop_runtime_session.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
            recorder = c.shared.Recorder(raw, out)
            with patch.object(c.shared.bench, "timed_invoke", recorder.timed_invoke):
                summary = c.run_burst(recorder, {"arn": "arn", "name": "test"}, "us-west-2", out)
            self.assertEqual(summary["samples"], 100)
            self.assertEqual(summary["success"], 100)
            self.assertEqual(summary["warm_success"], 100)
            self.assertEqual(summary["stop_success"], 100)
            self.assertEqual(raw.invoke_agent_runtime.call_count, 200)
            self.assertEqual(raw.stop_runtime_session.call_count, 100)
            result = json.loads((out / "raw" / "500mb_c100.json").read_text())
            self.assertEqual(result["meta"]["concurrency"], 100)
            self.assertEqual({r["request_idx"] for r in result["requests"]}, set(range(100)))
            self.assertEqual(len({r["session_id"] for r in result["requests"]}), 100)
            self.assertEqual(c.shared.CONCURRENCY, 200)

    def test_thread_start_failure_aborts_without_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            with patch.object(c.threading.Thread, "start", side_effect=RuntimeError("limit")), patch.object(
                    c.shared.bench, "probe") as probe:
                with self.assertRaises(RuntimeError):
                    c.run_burst(MagicMock(), {"arn": "arn", "name": "test"}, "us-west-2", out)
            probe.assert_not_called()
            self.assertEqual(json.loads((out / "raw/500mb_c100.json").read_text())["requests"], [])

    def test_prior_request_supports_private_api_and_has_expected_scope(self):
        from botocore.session import Session
        from botocore.validate import validate_parameters
        path = c.HERE / "results/coldstart_v2_c200_500mb_retest_2026-09-11/create_requests.json"
        requests = json.loads(path.read_text())
        self.assertEqual(set(requests), {"500mb"})
        request = requests["500mb"]
        validate_parameters(request, Session().get_service_model("bedrock-agentcore-control").operation_model(
            "CreateAgentRuntime").input_shape)
        self.assertEqual(request["platformVersion"], "V2")
        self.assertEqual(request["lifecycleConfiguration"], {"idleRuntimeSessionTimeout": 60, "maxLifetime": 600})
        self.assertIn("@sha256:", request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"])

    def test_mocked_run_creates_one_runtime_and_cleans_up(self):
        prior = c.HERE / "results/coldstart_v2_c200_500mb_retest_2026-09-11"
        request = json.loads((prior / "create_requests.json").read_text())["500mb"]
        run_meta = json.loads((prior / "run.json").read_text())
        image = json.loads((prior / "images.json").read_text())["500mb"]
        from botocore.session import Session
        control, ecr, sts, raw = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        control.meta.service_model = Session().get_service_model("bedrock-agentcore-control")
        control.create_agent_runtime.return_value = {"agentRuntimeId": "owned", "agentRuntimeArn": "arn"}
        ecr.describe_images.return_value = {"imageDetails": [image]}
        sts.get_caller_identity.return_value = {"Account": run_meta["account"]}
        session = MagicMock()
        session.client.side_effect = lambda service, **kw: {
            "sts": sts, "bedrock-agentcore-control": control, "ecr": ecr}[service]
        raw.meta.config.max_pool_connections = 200
        raw.meta.config.retries = {"total_max_attempts": 1}
        summary = {"success": 100, "warm_success": 100, "stop_success": 100}
        with tempfile.TemporaryDirectory() as tmp, patch.object(c.boto3, "Session", return_value=session), \
                patch.object(c.shared.bench, "make_client", return_value=raw) as make, \
                patch.object(c.shared, "read_quotas", return_value=[]), \
                patch.object(c.shared.original, "wait_ready", return_value={"platformVersion": "V2"}), \
                patch.object(c.shared.original, "wait_endpoint_ready"), \
                patch.object(c.shared.bench, "probe", return_value={"success": True, "warm_ms": 1, "stopped": True}), \
                patch.object(c, "run_burst", return_value=summary) as burst, \
                patch.object(c.time, "sleep"), \
                patch.object(c.shared.original, "cleanup", return_value=True) as cleanup:
            out = Path(tmp)
            self.assertEqual(c.run(out), 0)
            make.assert_called_once_with("us-west-2", 100)
            control.create_agent_runtime.assert_called_once()
            actual = control.create_agent_runtime.call_args.kwargs
            for field in ["agentRuntimeArtifact", "roleArn", "lifecycleConfiguration", "platformVersion"]:
                self.assertEqual(actual[field], request[field])
            burst.assert_called_once()
            self.assertEqual(set(cleanup.call_args.args[1]["runtimes"]), {"500mb"})
            saved = json.loads((out / "run.json").read_text())
            self.assertTrue(saved["measurement_complete"] and saved["cleanup_complete"])
            self.assertEqual(saved["concurrency"], 100)




if __name__ == "__main__":
    unittest.main()
