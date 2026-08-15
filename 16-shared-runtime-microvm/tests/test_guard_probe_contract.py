"""Keep the deterministic path-guard probe protocol synchronized."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
MARKER = "[PATH-GUARD-INTEGRATION-PROBE]"


class TestGuardProbeContract(unittest.TestCase):
    def test_server_and_smoke_share_exact_probe_marker(self):
        server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts" / "invoke_multiuser.py").read_text(encoding="utf-8")
        self.assertEqual(server.count(MARKER), 1)
        self.assertEqual(smoke.count(MARKER), 1)
        self.assertIn("ClaudeSDKClient", server)
        self.assertIn("client.receive_response()", server)
        self.assertNotIn("async for message in query(", server)
        self.assertIn("denied_count", smoke)
        self.assertIn("not leaked", smoke)


if __name__ == "__main__":
    unittest.main()
