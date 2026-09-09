"""Offline tests: no AWS calls, no real certificates or shell commands."""
import asyncio
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import agent

BUNDLE = {"server": "https://10.0.0.10:6443", "server_ca": "server-ca",
          "client_cert": "cert", "client_key": "key", "untrusted_cert": "bad-cert",
          "untrusted_key": "bad-key", "wrong_ca": "bad-ca"}


class AgentTests(unittest.TestCase):
    def test_modes_and_cleanup(self):
        for mode in agent.MODES:
            paths = []

            def fake_run(argv, **kwargs):
                path = Path(argv[argv.index("--kubeconfig") + 1])
                paths.append(path)
                config = json.loads(path.read_text())
                cluster = config["clusters"][0]["cluster"]
                self.assertNotIn("insecure-skip-tls-verify", cluster)
                self.assertEqual(Path(cluster["certificate-authority"]).read_text(),
                                 "bad-ca" if mode == "wrong_server_ca" else "server-ca")
                user = config["users"][0]["user"]
                if mode == "no_client_cert":
                    self.assertEqual(user, {})
                else:
                    self.assertEqual(set(user), {"client-key", "client-certificate"})
                    self.assertEqual(Path(user["client-certificate"]).read_text(),
                                     "bad-cert" if mode == "untrusted_client_cert" else "cert")
                    key = Path(user["client-key"])
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(key.read_text(), "bad-key" if mode == "untrusted_client_cert" else "key")
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                return subprocess.CompletedProcess(argv, 0, "{}", "")

            with patch("agent.subprocess.run", side_effect=fake_run), patch("agent.no_certificate_probe", return_value={"http_status": 401}):
                self.assertEqual(agent.execute_kubectl(BUNDLE, "nodes", mode)["returncode"], 0)
            self.assertFalse(paths[0].exists())

    def test_temporary_files_removed_on_failure(self):
        for error in [FileNotFoundError("kubectl"), subprocess.TimeoutExpired("kubectl", 65)]:
            paths = []

            def fail(argv, **kwargs):
                paths.append(Path(argv[argv.index("--kubeconfig") + 1]))
                raise error

            with patch("agent.subprocess.run", side_effect=fail), self.assertRaises(type(error)):
                agent.execute_kubectl(BUNDLE, "nodes", "valid")
            self.assertFalse(paths[0].exists())

    def test_reject_arbitrary_operations(self):
        for op in ["nodes; id", "delete", "exec", "--kubeconfig=/etc/config"]:
            with self.assertRaises(ValueError):
                agent.execute_kubectl(BUNDLE, op, "valid")

    def test_verify_does_not_create_llm(self):
        with patch("agent.get_bundle", return_value=BUNDLE), patch("agent.execute_kubectl", return_value={"ok": True}), patch("agent.Agent") as llm:
            self.assertEqual(asyncio.run(agent.invoke({"action": "verify"})), {"ok": True})
            llm.assert_not_called()

    def test_invalid_mode_rejected_before_secret_fetch(self):
        with patch("agent.get_bundle") as get_secret:
            with self.assertRaises(ValueError):
                asyncio.run(agent.invoke({"mode": "arbitrary"}))
            get_secret.assert_not_called()


if __name__ == "__main__":
    unittest.main()
