"""Offline safety and evidence checks; all cloud clients are mocked."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import memory_v2 as m


class MemoryV2Tests(unittest.TestCase):
    def test_state_isolation(self):
        old_lab, old_analyze = m.lab.STATE, m.analyze.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                m.bind_output(out)
                self.assertEqual(m.lab.STATE, out)
                self.assertEqual(m.analyze.STATE, out)
                m.save({"sessions": []})
                self.assertEqual(m.lab.load_state(), {"sessions": []})
        finally:
            m.lab.STATE, m.analyze.STATE = old_lab, old_analyze

    def test_exact_ten_calls_and_alternating_order(self):
        with patch.object(m, "invoke_case") as invoke:
            m.run_cases({})
        calls = [(c.args[1], c.args[2]) for c in invoke.call_args_list]
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(set(calls)), 10)
        self.assertEqual(calls[:4], [("default_small", "baseline"), ("v2_small", "baseline"),
                                    ("v2_small", "anonymous"), ("default_small", "anonymous")])

    def test_invoke_failure_still_stops_without_retry(self):
        state = {"sessions": [], "runtimes": {"v2_small": {"arn": "arn"}}}
        data = MagicMock()
        data.invoke_agent_runtime.side_effect = RuntimeError("failure")
        with patch.object(m, "client", return_value=data), patch.object(m, "save"):
            with self.assertRaises(RuntimeError):
                m.invoke_case(state, "v2_small", "baseline")
        data.invoke_agent_runtime.assert_called_once()
        data.stop_runtime_session.assert_called_once()

    def test_result_write_failure_still_stops(self):
        state = {"sessions": [], "runtimes": {"v2_small": {"arn": "arn"}}}
        data = MagicMock()
        data.invoke_agent_runtime.return_value = {"ResponseMetadata": {}, "response": MagicMock()}
        data.invoke_agent_runtime.return_value["response"].read.return_value = b'{}'
        with patch.object(m, "client", return_value=data), patch.object(m, "save"), patch.object(
                m.lab, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.invoke_case(state, "v2_small", "baseline")
        data.stop_runtime_session.assert_called_once()

    def test_cleanup_never_deletes_controls(self):
        state = {"runtimes": {"default_small": {"id": "keep", "owned": False},
                              "v2_small": {"id": "delete", "owned": True}}}
        ctl = MagicMock()
        ctl.get_agent_runtime.side_effect = ClientError({"Error": {
            "Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
        with patch.object(m, "client", return_value=ctl), patch.object(m, "save"):
            self.assertTrue(m.cleanup(state))
        ctl.delete_agent_runtime.assert_called_once_with(agentRuntimeId="delete")
        self.assertTrue(state["runtimes"]["v2_small"]["deleted"])

    def test_unknown_create_outcome_not_claimed_clean(self):
        state = {"runtimes": {"v2_small": {"owned": True, "create_started": 123}}}
        ctl = MagicMock()
        with patch.object(m, "client", return_value=ctl), patch.object(m, "save"):
            self.assertFalse(m.cleanup(state))
        ctl.delete_agent_runtime.assert_not_called()

    def test_missing_platform_not_treated_as_v2(self):
        rt = {"status": "READY", "agentRuntimeArtifact": {}, "roleArn": "role",
              "networkConfiguration": {}, "protocolConfiguration": {}, "lifecycleConfiguration": {}}
        m.validate_runtime(rt, rt, "default")
        with self.assertRaises(AssertionError):
            m.validate_runtime(rt, rt, "V2")
        m.validate_runtime({**rt, "platformVersion": "V2"}, rt, "V2")

    def test_real_baseline_spacing_and_incomplete_arm_count(self):
        root = m.BASELINE / ".state"
        state = json.loads((root / "resources.json").read_text())
        telemetry = json.loads((root / "telemetry.json").read_text())
        with patch.object(m.analyze, "STATE", root):
            report = m.assess(state, telemetry)
        self.assertFalse(report["complete"])  # only 5 historical sessions, not the approved 10
        self.assertEqual(report["parse_errors"], [])
        self.assertTrue(all(p["aws"]["continuous"] for s in report["sessions"] for p in s["phases"]))

    def test_equal_coverage_cannot_hide_timestamp_gap(self):
        phase = {"window_start": 0, "window_end": 24, "aws": {"coverage_fraction": 1}}
        summary = {"sessions": [{"session_id": "s", "phases": [phase]}], "parse_errors": []}
        records = [{"session_id": "s", "timestamp": t, "elapsed_seconds": 1}
                   for t in list(range(1, 12)) + list(range(13, 26))]
        with patch.object(m.analyze, "summarize", return_value=deepcopy(summary)), patch.object(
                m.analyze, "parse_events", return_value=(records, [])):
            report = m.assess({}, {"runtimes": {"r": {"events": []}}})
        self.assertFalse(report["sessions"][0]["phases"][0]["aws"]["continuous"])



if __name__ == "__main__":
    unittest.main()
