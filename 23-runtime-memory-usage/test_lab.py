"""AWS control code tests use mocks; no AWS calls."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import lab


class LabTests(unittest.TestCase):
    def test_collect_interrupted_session_and_long_window(self):
        state = {"region": "us-west-2", "sessions": [{"id": "x", "start": 1000},
                 {"id": "y", "start": 180000, "end": 180010}],
                 "runtimes": {"small": {"arn": "test-arn", "usage_log_group": "test-group"}}}
        logs, cw = MagicMock(), MagicMock()
        logs.get_paginator.return_value.paginate.return_value = [{"events": []}]
        cw.get_paginator.return_value.paginate.return_value = [{"Metrics": [{
            "Namespace": "AWS/Bedrock-AgentCore", "MetricName": "MemoryUsed-GBHours", "Dimensions": []}]}]
        cw.get_metric_statistics.return_value = {"Datapoints": []}
        with patch.object(lab, "load_state", return_value=state), patch.object(
                lab, "client", side_effect=[logs, cw]), patch.object(lab, "write_json") as write:
            result = lab.collect(SimpleNamespace())
        self.assertEqual(result["runtimes"]["small"]["events"], [])
        self.assertLessEqual((result["end"] - result["start"]) /
                             cw.get_metric_statistics.call_args.kwargs["Period"], 1440)
        write.assert_called_once()

    def test_failed_invocation_still_stops_session(self):
        state = {"region": "us-west-2", "sessions": [], "runtimes": {"small": {"arn": "arn"}}}
        data = MagicMock()
        data.invoke_agent_runtime.side_effect = RuntimeError("test failure")
        with patch.object(lab, "load_state", return_value=state), patch.object(
                lab, "client", return_value=data), patch.object(lab, "save"):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                lab.run(SimpleNamespace(phase_seconds=30))
        data.stop_runtime_session.assert_called_once()
        self.assertIn("end", state["sessions"][0])
        self.assertIn("error", state["sessions"][0])


if __name__ == "__main__":
    unittest.main()
