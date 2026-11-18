"""SSM orchestration offline tests: never submit any real AWS operation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("ssm_command", Path(__file__).resolve().parents[1] / "scripts/ssm_command.py")
ssm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssm)

class SSMTests(unittest.TestCase):
    def test_terminal_failure_never_resubmits(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ssm, "aws", return_value={
                "Status": "Failed", "ResponseCode": 1, "StandardOutputContent": "failure"}) as api, \
                self.assertRaises(RuntimeError):
            ssm.wait("region", "instance", "command", Path(tmp), 10)
        api.assert_called_once()
        self.assertEqual(api.call_args.args[1:3], ("ssm", "get-command-invocation"))

    def test_delayed_read_then_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ssm, "aws", side_effect=[
                {"Status": "InProgress"}, {"Status": "Success", "ResponseCode": 0}]) as api, \
                patch.object(ssm.time, "sleep"):
            ssm.wait("region", "instance", "command", Path(tmp), 10)
            self.assertTrue((Path(tmp) / "command.json").exists())
        self.assertEqual(api.call_count, 2)

    def test_ambiguous_submit_saved_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ssm, "aws", side_effect=TimeoutError()) as api, \
                self.assertRaises(TimeoutError):
            ssm.send("region", "instance", "echo safe", "test", Path(tmp), 60)
        api.assert_called_once()

    def test_missing_invocation_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ssm, "aws", side_effect=RuntimeError("InvocationDoesNotExist")) as api, \
                patch.object(ssm.time, "sleep"), self.assertRaises(RuntimeError):
            ssm.wait("region", "instance", "command", Path(tmp), 10)
        self.assertEqual(api.call_count, 6)

    def test_other_permission_error_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ssm, "aws", side_effect=RuntimeError("AccessDeniedException")) as api, \
                self.assertRaises(RuntimeError):
            ssm.wait("region", "instance", "command", Path(tmp), 10)
        api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
