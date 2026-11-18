"""Offline contracts: no AWS calls, TAPs or Nitro device required."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

class BenchmarkTests(unittest.TestCase):
    def sample(self, **overrides):
        data = dict(run_id="run", boot_id="boot", ok=True, exit_code=0, timed_out=False,
                    stop_reason="completed", dsh_first_event_ms=1., dsh_first_text_ms=2., dsh_total_ms=3.)
        return {**data, **overrides}

    def test_nitro_config_not_kvm(self):
        args = bench.parser().parse_args([])
        cfg = bench.config(args, "fresh", "tap-test")
        self.assertEqual(cfg["drives"], [])
        self.assertTrue(cfg["enclave"]["cpu_overcommit"])
        self.assertFalse(cfg["enclave"]["direct_mode"])
        self.assertFalse(cfg["enclave"]["debug_mode"])
        self.assertIn("bench_id=fresh", cfg["boot-source"]["boot_args"])
        self.assertEqual(cfg["machine-config"]["mem_size_mib"], 4096)

    def test_percentiles_and_failures(self):
        rows = [dict(round=i, phase="cold", ok=True, sandbox_ready_ms=float(i)) for i in range(1, 11)]
        rows.append(dict(round=11, phase="cold", ok=False, sandbox_ready_ms=999.))
        summary = bench.summarize(rows)
        self.assertEqual(summary["cold"]["attempted"], 11)
        self.assertEqual(summary["cold"]["failed"], 1)
        self.assertEqual(summary["cold"]["metrics_ms"]["sandbox_ready_ms"]["p50"], 5.)
        self.assertEqual(summary["cold"]["metrics_ms"]["sandbox_ready_ms"]["p90"], 9.)
        self.assertEqual(summary["cold"]["metrics_ms"]["sandbox_ready_ms"]["mean"], 5.5)
        self.assertEqual(summary["warm"]["metrics_ms"]["request_ms"], {"n": 0})

    def test_result_identity_timing_and_completion(self):
        self.assertTrue(bench.validate_result(self.sample(), "run", "boot", "agent")["ok"])
        for bad in (self.sample(boot_id="other"), self.sample(dsh_total_ms=-1),
                    self.sample(dsh_first_event_ms=float("nan")), self.sample(dsh_first_text_ms=4),
                    self.sample(dsh_first_event_ms=True)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bench.validate_result(bad, "run", "boot", "agent")
        self.assertFalse(bench.validate_result(self.sample(stop_reason="failed"), "run", "boot", "agent")["ok"])
        self.assertFalse(bench.validate_result(self.sample(dsh_first_text_ms=None), "run", "boot", "agent")["ok"])
        result = bench.validate_result(self.sample(credentials="secret", output="secret", error="secret"),
                                       "run", "boot", "agent")
        self.assertNotIn("secret", json.dumps(result))

    def test_firewall_offline_and_metadata_protection(self):
        offline = bench.firewall_rules("dshb_test", "dshtest", False, "")
        self.assertNotIn("masquerade", offline)
        self.assertIn('iifname "dshtest" reject', offline)
        online = bench.firewall_rules("dshb_test", "dshtest", True, "eth0")
        self.assertLess(online.index("169.254.169.254 reject"), online.index("tcp dport 443 accept"))
        self.assertLess(online.index("169.254.169.253 udp dport 53 accept"), online.index("169.254.0.0/16"))
        self.assertIn("meta nfproto ipv6 reject", online)
        self.assertIn("masquerade", online)

    def test_input_bounds(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(bench.argparse.ArgumentTypeError):
                bench.positive_float(value)

    def test_readiness_rejects_stale_vm_and_early_exit(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(bench, "request", return_value={"run_id": "old", "boot_id": "boot"}), self.assertRaises(RuntimeError):
            bench.wait_ready(proc, "new", bench.time.perf_counter(), 1)
        proc.poll.return_value = 1
        with patch.object(bench, "request") as call, self.assertRaises(RuntimeError):
            bench.wait_ready(proc, "new", bench.time.perf_counter(), 1)
        call.assert_not_called()

    def test_readiness_recovers_read_only_probe(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        health = {"run_id": "run", "boot_id": "boot", "dsh_version": "test"}
        with patch.object(bench, "request", side_effect=[OSError(), health]) as call:
            self.assertEqual(bench.wait_ready(proc, "run", bench.time.perf_counter(), 1), health)
        self.assertEqual(call.call_count, 2)

    def test_round_does_not_retry_ambiguous_invocation(self):
        from unittest.mock import Mock
        args = bench.parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs = root / "sysfs"
            sysfs.mkdir()
            (sysfs / "pcie_slots_in_use").write_text("1")
            proc = Mock()
            proc.poll.return_value = None
            health = {"run_id": "run", "boot_id": "boot", "dsh_version": "test"}
            def stop(_):
                (sysfs / "pcie_slots_in_use").write_text("0")
            with patch.object(bench.uuid, "uuid4") as uid, patch.object(bench, "SYSFS", sysfs), \
                    patch.object(bench.subprocess, "Popen", return_value=proc), \
                    patch.object(bench, "wait_ready", return_value=health), \
                    patch.object(bench, "stop", side_effect=stop), \
                    patch.object(bench, "request", side_effect=TimeoutError()) as invoke:
                uid.return_value.hex = "run"
                rows = bench.run_round(args, 1, "tap-test", root, {})
            invoke.assert_called_once()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["phase"], "cold")
            self.assertFalse(rows[0]["ok"])

    def test_credentials_permissions_expiry_and_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "creds"
            file.write_text(json.dumps(dict(AccessKeyId="test", SecretAccessKey="secret", SessionToken="test-token",
                                           Expiration="2099-01-01T00:00:00Z", untrusted="drop")))
            file.chmod(0o600)
            self.assertEqual(set(bench.credentials(file)), {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"})
            file.chmod(0o644)
            with self.assertRaises(ValueError):
                bench.credentials(file)
            file.chmod(0o600)
            file.write_text(json.dumps(dict(AccessKeyId="test", SecretAccessKey="secret", SessionToken="token", Expiration="2000-01-01T00:00:00Z")))
            with self.assertRaises(ValueError):
                bench.credentials(file)

    def test_manifest_checks_kernel_architecture_and_hashes(self):
        args = bench.parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            args.kernel, args.initrd, args.manifest = (Path(tmp) / name for name in ("kernel", "initrd", "manifest"))
            args.kernel.write_bytes(b"kernel fixture")
            args.initrd.write_bytes(b"initrd fixture")
            manifest = dict(kernel_release="6.18.51-120.162.amzn2023.aarch64",
                kernel_sha256=bench.digest(args.kernel)["sha256"], initrd_sha256=bench.digest(args.initrd)["sha256"])
            args.manifest.write_text(json.dumps(manifest))
            with patch.object(bench.platform, "machine", return_value="aarch64"):
                self.assertEqual(bench.image_manifest(args)["build_manifest"], manifest)
                args.kernel.write_bytes(b"changed artifact")
                with self.assertRaisesRegex(ValueError, "hashes"):
                    bench.image_manifest(args)
            manifest["kernel_release"] = "6.12.1-1.1.amzn2023.aarch64"
            args.manifest.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "minimum"):
                bench.image_manifest(args)

    def test_forwarding_restored_if_tap_cleanup_fails(self):
        from unittest.mock import Mock
        forward = Mock()
        forward.read_text.return_value = "0"
        def cmd(*args):
            if args == ("ip", "-j", "route", "show", "default"):
                return json.dumps([dict(dev="eth0")])
            if args == ("nft", "-j", "list", "tables"):
                return json.dumps(dict(nftables=[]))
            if args[:3] == ("ip", "link", "delete"):
                raise RuntimeError("fixture deletion failure")
            return ""
        with patch.object(bench, "Path", return_value=forward), patch.object(bench, "command", side_effect=cmd), \
                patch.object(bench.subprocess, "run"), self.assertRaisesRegex(RuntimeError, "deletion failure"):
            with bench.network(True):
                pass
        self.assertEqual([call.args[0] for call in forward.write_text.call_args_list], ["1\n", "0\n"])

    def test_successful_round_reconciles_and_survives_cleanup_failure(self):
        from unittest.mock import Mock
        args = bench.parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sysfs = root / "sysfs"
            sysfs.mkdir()
            (sysfs / "pcie_slots_in_use").write_text("1")
            health = dict(run_id="run", boot_id="boot", dsh_version="test")
            with patch.object(bench.uuid, "uuid4") as uid, patch.object(bench, "SYSFS", sysfs), \
                    patch.object(bench.subprocess, "Popen", return_value=Mock()), \
                    patch.object(bench, "wait_ready", return_value=health), \
                    patch.object(bench, "request", return_value=self.sample()), \
                    patch.object(bench.time, "perf_counter", side_effect=[10, 11, 11.1, 12, 12.1, 13]), \
                    patch.object(bench, "stop", side_effect=RuntimeError("cleanup failure")), \
                    self.assertRaisesRegex(RuntimeError, "cleanup failure"):
                uid.return_value.hex = "run"
                bench.run_round(args, 1, "tap-test", root, {})
            rows = json.loads((root / "round-0001/records.json").read_text())
            self.assertEqual([r["phase"] for r in rows], ["cold", "warm"])
            self.assertTrue(all(r["ok"] for r in rows))
            cold = rows[0]
            self.assertAlmostEqual(cold["launch_to_response_ms"], cold["sandbox_ready_ms"]
                                   + cold["ready_to_invoke_ms"] + cold["request_ms"])
            self.assertIsNone(rows[1]["sandbox_ready_ms"])
            self.assertIsNone(rows[1]["launch_to_response_ms"])


    def test_completed_reply_is_separate_from_exact_answer(self):
        row = dict(round=1, phase="cold", workload="agent", ok=False, exit_code=0,
                   timed_out=False, stop_reason="completed", dsh_first_text_ms=2,
                   request_ms=3, answer_matches_expected=False)
        self.assertTrue(bench.completed(row))
        self.assertTrue(bench.answer_mismatch(row))
        self.assertEqual(bench.summarize([row])["cold"]["failed"], 1)
        self.assertEqual(bench.completion_summary([row])["cold"]["failed"], 0)
        self.assertFalse(bench.answer_mismatch({**row, "timed_out": True}))
        self.assertFalse(bench.answer_mismatch({**row, "stop_reason": "failed"}))

    def test_opt_in_quality_guard_is_bounded_and_execution_failure_stops(self):
        import contextlib
        from unittest.mock import MagicMock
        for failure in ("answer", "execution"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sysfs = root / "sysfs"
                sysfs.mkdir()
                (sysfs / "pcie_slots_in_use").write_text("0")
                row = dict(phase="cold", workload="agent", ok=False, exit_code=0,
                           timed_out=False, stop_reason="completed", dsh_first_text_ms=2,
                           request_ms=3, answer_matches_expected=False)
                if failure == "execution":
                    row["exit_code"] = 1
                def group(args, number, *unused):
                    return [{**row, "round": number}]
                argv = ["benchmark.py", "--workload", "agent", "--direct-mode", "--credentials-file", "unused",
                        "--region", "us-east-1", "--model", "test", "--rounds", "10",
                        "--continue-on-answer-mismatch", "--out", str(root / "results")]
                with patch("sys.argv", argv), patch.object(bench.os, "geteuid", return_value=0), \
                        patch.object(bench, "open", create=True, return_value=MagicMock()), \
                        patch.object(bench.fcntl, "flock"), patch.object(bench, "SYSFS", sysfs), \
                        patch.object(bench, "preflight", return_value={"build_manifest": {}}), \
                        patch.object(bench, "credentials", return_value={}), \
                        patch.object(bench, "network", return_value=contextlib.nullcontext("dshtest")), \
                        patch.object(bench, "run_round", side_effect=group) as run, patch("builtins.print"):
                    self.assertEqual(bench.main(), 1)
                self.assertEqual(run.call_count, 3 if failure == "answer" else 1)



    def test_process_timeout_cleanup_escalates(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("firecracker", 5), 0]
        bench.stop(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)


@unittest.skipUnless(bench.shutil.which("node"), "Node needed for guest integration tests")
class GuestHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import socket
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        # Only the fixture is copied into a test-owned directory and made executable.
        fake = Path(cls.tmp.name) / "dsh"
        fake.write_bytes((ROOT / "tests/fake_dsh.mjs").read_bytes())
        fake.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "HOST": "127.0.0.1", "PORT": str(port),
               "BENCH_RUN_ID": "test-run", "DSH_BIN": str(fake),
               "DSH_HOME": cls.tmp.name + "/home", "DSH_WORKDIR": cls.tmp.name}
        cls.proc = subprocess.Popen(["node", str(ROOT / "guest/server.mjs")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.addClassCleanup(cls.cleanup)
        deadline = bench.time.perf_counter() + 5
        while bench.time.perf_counter() < deadline:
            try:
                cls.health = bench.request(cls.url + "/health")
                return
            except OSError:
                if cls.proc.poll() is not None:
                    raise RuntimeError("guest fixture failed to start")
                bench.time.sleep(.01)
        raise TimeoutError("guest fixture readiness timeout")

    @classmethod
    def cleanup(cls):
        bench.stop(cls.proc)
        cls.proc.communicate(timeout=5)
        cls.tmp.cleanup()

    def invoke(self, prompt="pong", timeout=1000, expected=""):
        return bench.request(self.url + "/invoke", dict(mode="agent", region="us-east-1", model="test",
            prompt=prompt, timeout_ms=timeout, expected_text=expected, credentials={"AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "test-token"}), timeout=3)

    def test_answer_correctness_and_token_limit(self):
        self.assertTrue(self.invoke(expected="pong")["answer_matches_expected"])
        data = self.invoke(expected="different")
        self.assertFalse(data["answer_matches_expected"])
        self.assertFalse(data["ok"])
        profile = Path(self.tmp.name) / "home/profiles/headless/cordis.patch.yml"
        self.assertIn("maxTokens: 512", profile.read_text())

    def test_startup_no_model_credentials(self):
        data = bench.request(self.url + "/invoke", dict(mode="startup", timeout_ms=1000))
        self.assertTrue(data["ok"])
        self.assertIsNone(data["dsh_first_text_ms"])
        self.assertGreater(data["dsh_total_ms"], 0)

    def test_split_ndjson_unicode_and_final_tail(self):
        data = self.invoke()
        self.assertTrue(data["ok"])
        self.assertEqual(data["run_id"], "test-run")
        self.assertGreaterEqual(data["dsh_first_text_ms"], data["dsh_first_event_ms"])
        self.assertGreaterEqual(data["dsh_total_ms"], data["dsh_first_text_ms"])
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("pong", json.dumps(data))

    def test_provider_failure_and_missing_text_are_failures(self):
        for prompt in ("failed", "missing-text"):
            with self.subTest(prompt=prompt):
                data = self.invoke(prompt)
                self.assertFalse(data["ok"])
                self.assertNotIn("secret-from-provider", json.dumps(data))

    def test_timeout_and_followup(self):
        data = self.invoke("timeout", 30)
        self.assertFalse(data["ok"])
        self.assertTrue(data["timed_out"])
        self.assertTrue(self.invoke()["ok"])

    def test_output_limit_and_followup(self):
        data = self.invoke("flood")
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "output limit exceeded")
        self.assertTrue(self.invoke()["ok"])

    def test_invalid_input_rejected(self):
        for body in (dict(mode="unknown", timeout_ms=1000), dict(mode="startup", timeout_ms=-1),
                     dict(mode="agent", prompt="pong", timeout_ms=1000)):
            with self.subTest(body=body), self.assertRaises(bench.urllib.error.HTTPError):
                bench.request(self.url + "/invoke", body)



if __name__ == "__main__":
    unittest.main()
