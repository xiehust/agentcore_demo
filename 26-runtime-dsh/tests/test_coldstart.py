"""Offline checks for the cold-start benchmark client; no AWS calls."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

import boto3
import botocore.session
from botocore.config import Config
from botocore.validate import ParamValidator

from fakes import PLATFORM_DEPLOYMENTS, FakeClock, FakeRuntime, fake_factory, write_deployments
import coldstart_test as ct

ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/dsh_base-AAAA"


class ProbeTests(unittest.TestCase):
    """One probe at a time on a fake clock, so every duration is exact."""

    def probe(self, sequence: str, behavior: dict, platform_version=None):
        clock = FakeClock()
        self.fake = FakeRuntime(clock, [behavior])
        with patch.object(ct, "time", clock):
            (cold, warm), session = ct.probe(self.fake, "dsh", ARN, sequence, "say pong", 1, 1, 0, None,
                                             platform_version)
        return cold, warm, session

    def test_client_side_delays_are_recorded(self):
        clock = FakeClock()
        fake = FakeRuntime(clock, [{"boot": "pool", "connect": 0.02}], client_delay=0.004)
        self.assertTrue(ct.instrument(fake))
        with patch.object(ct, "time", clock):
            requests, _ = ct.run_round(fake, "dsh", ARN, "ping", "", 1, 1)
        cold, warm = requests
        # 4 ms to serialize and sign, 20 ms to connect: both are inside the measured latency
        self.assertEqual((cold["start_to_send_ms"], cold["connect_ms"], cold["release_to_start_ms"]),
                         (4.0, 20.0, 0.0))
        self.assertEqual(cold["total_ms"], 181.0)  # 157 ms without those two
        self.assertEqual((warm["start_to_send_ms"], warm["connect_ms"], warm["release_to_start_ms"]),
                         (4.0, None, None))        # the warm call reuses the connection
        self.assertNotIn("t0_perf", cold)
        # an uninstrumented client still works; the marks are simply absent
        with patch.object(ct, "time", clock):
            (cold, _), _ = ct.probe(FakeRuntime(clock), "dsh", ARN, "ping", "", 1, 1, 0, None)
        self.assertEqual((cold["ok"], cold["start_to_send_ms"], cold["release_to_start_ms"]), (True, None, None))

    def test_probe_that_is_never_released_is_recorded_as_not_sent(self):
        class NeverReleased:
            def wait(self):
                return False
        fake = FakeRuntime(FakeClock())
        (cold, warm), session = ct.probe(fake, "dsh", ARN, "ping", "", 4, 1, 2, NeverReleased(),
                                         session_id="s" * 40, worker=1)
        self.assertEqual((cold["attempted"], cold["ok"], cold["error_type"]), (False, False, "not_sent"))
        self.assertEqual((warm["attempted"], warm["error_type"]), (False, "skipped"))
        self.assertEqual((cold["session_id"], cold["worker"], cold["probe_idx"]), ("s" * 40, 1, 2))
        self.assertEqual((fake.invokes, fake.stops, session["stopped"]), ([], [], False))

    def test_snapshot_restore_on_platform_v2(self):
        behavior = {"boot": "snapshot", "cold_dsh_boot": 40.0}
        cold, warm, _ = self.probe("agent", behavior, "V2")
        self.assertEqual((cold["platform_version"], cold["boot_kind"], warm["boot_kind"]),
                         ("V2", ct.BOOT_SNAPSHOT, ct.BOOT_SNAPSHOT))
        self.assertEqual((cold["total_ms"], cold["dsh_boot_ms"], cold["dsh_total_ms"]),
                         (43720.0, 40000.0, 41800.0))
        self.assertEqual((cold["platform_overhead_ms"], cold["outside_handler_ms"]), (1920.0, 1905.0))
        self.assertEqual(cold["uptime_at_request_ms"], 301900.0)  # the snapshot's process is old
        self.assertEqual((cold["invocation_seq"], cold["anomalies"]), (1, []))
        # The same timestamps on a runtime without that platform version are a warm-pool hit.
        cold, _, _ = self.probe("agent", behavior)
        self.assertEqual((cold["platform_version"], cold["boot_kind"]), (None, ct.BOOT_POOL))

    def test_real_boot_ping(self):
        cold, warm, session = self.probe("ping", {"boot": "real"})
        self.assertTrue(cold["ok"] and warm["ok"])
        self.assertEqual((cold["headers_ms"], cold["total_ms"], cold["status_code"]), (6002.0, 6007.0, 200))
        self.assertEqual(cold["uptime_at_request_ms"], 500.0)
        self.assertEqual((cold["server_handling_ms"], cold["outside_handler_ms"]), (2.0, 6005.0))
        self.assertEqual(cold["boot_kind"], ct.BOOT_REAL)
        self.assertEqual((warm["headers_ms"], warm["total_ms"]), (32.0, 37.0))
        self.assertEqual(warm["boot_kind"], ct.BOOT_REAL)  # inherited from the session
        self.assertEqual((cold["invocation_seq"], warm["invocation_seq"]), (1, 2))
        self.assertEqual(cold["anomalies"] + warm["anomalies"], [])
        self.assertEqual(cold["server"]["image_tag"], "dsh-test")  # full server JSON kept
        self.assertIsNone(cold["dsh_total_ms"])
        # request shape: fresh 33+ char session shared by both invokes, then stopped
        self.assertEqual(len(cold["session_id"]), 40)
        self.assertEqual([json.loads(i["payload"]) for i in self.fake.invokes], [{"mode": "ping"}] * 2)
        self.assertEqual({i["runtimeSessionId"] for i in self.fake.invokes}, {cold["session_id"]})
        self.assertEqual(self.fake.stops, [{"agentRuntimeArn": ARN, "qualifier": "DEFAULT",
                                            "runtimeSessionId": cold["session_id"]}])
        self.assertEqual(session, {"session_id": cold["session_id"], "stopped": True, "stop_error": None})

    def test_real_boot_agent(self):
        cold, warm, _ = self.probe("agent", {"boot": "real"})
        self.assertEqual(json.loads(self.fake.invokes[0]["payload"]),
                         {"mode": "agent", "prompt": "say pong"})
        self.assertEqual((cold["total_ms"], cold["boot_kind"]), (9020.0, ct.BOOT_REAL))
        self.assertEqual((cold["dsh_boot_ms"], cold["first_text_ms"], cold["dsh_total_ms"]),
                         (1200.0, 2000.0, 3000.0))
        self.assertEqual(cold["platform_overhead_ms"], 6020.0)
        self.assertEqual((warm["dsh_boot_ms"], warm["dsh_total_ms"], warm["total_ms"]),
                         (400.0, 1500.0, 1550.0))

    def test_pool_hit_with_long_agent_turn_is_not_a_real_boot(self):
        # The process had been up for 2 s when the request was sent (2.15 s when
        # it arrived); the turn takes 3 s. Comparing uptime with the client
        # total (demo 10's rule) would call this a real boot.
        cold, warm, _ = self.probe("agent", {"boot": "pool", "pool_age": 2.0})
        self.assertEqual((cold["uptime_at_request_ms"], cold["total_ms"]), (2150.0, 3170.0))
        self.assertLess(cold["uptime_at_request_ms"], cold["total_ms"])
        self.assertEqual(cold["boot_kind"], ct.BOOT_POOL)
        self.assertEqual(warm["boot_kind"], ct.BOOT_POOL)
        self.assertEqual((cold["platform_overhead_ms"], cold["outside_handler_ms"]), (170.0, 155.0))

    def test_throttled_cold_is_recorded_and_warm_is_not_sent(self):
        cold, warm, session = self.probe("ping", {"fail": "throttle"})
        self.assertFalse(cold["ok"])
        self.assertEqual((cold["error_type"], cold["status_code"]), ("throttle", 429))
        self.assertEqual((cold["total_ms"], cold["headers_ms"]), (50.0, None))
        self.assertEqual(cold["boot_kind"], ct.BOOT_UNKNOWN)
        self.assertEqual((warm["attempted"], warm["ok"], warm["error_type"]), (False, False, "skipped"))
        self.assertEqual(len(self.fake.invokes), 1)
        self.assertEqual(len(self.fake.stops), 1)  # still best-effort stopped
        self.assertTrue(session["stopped"])

    def test_other_cold_failures(self):
        for fail, kind, status in (("http500", "http_error", 500), ("timeout", "timeout", None)):
            with self.subTest(fail=fail):
                cold, warm, _ = self.probe("agent", {"fail": fail})
                self.assertEqual((cold["ok"], cold["error_type"], cold["status_code"]), (False, kind, status))
                self.assertFalse(warm["attempted"])
        cold, warm, _ = self.probe("ping", {"fail": "bad_body"})
        self.assertEqual((cold["ok"], cold["error_type"], cold["status_code"]), (False, "bad_body", 200))
        self.assertEqual(cold["body_text"], "<html>upstream error</html>")
        self.assertFalse(warm["attempted"])

    def test_agent_ok_false_is_a_failure_that_keeps_its_evidence(self):
        cold, warm, _ = self.probe("agent", {"boot": "pool", "fail": "agent_fail"})
        self.assertEqual((cold["ok"], cold["error_type"], cold["status_code"]), (False, "agent_failed", 200))
        self.assertIn("exit_code=1", cold["error_msg"])
        self.assertEqual(cold["server"]["agent"]["ok"], False)
        self.assertEqual(cold["boot_kind"], ct.BOOT_POOL)  # the container did answer
        self.assertTrue(warm["attempted"] and warm["ok"])   # the microVM is up: warm is valid

    def test_warm_failure_does_not_touch_the_cold_sample(self):
        cold, warm, _ = self.probe("ping", {"warm_fail": "http500"})
        self.assertTrue(cold["ok"])
        self.assertEqual((warm["attempted"], warm["ok"], warm["error_type"]), (True, False, "http_error"))

    def test_stop_failure_is_recorded(self):
        _, _, session = self.probe("ping", {"stop_fail": True})
        self.assertFalse(session["stopped"])
        self.assertIn("ResourceNotFoundException", session["stop_error"])

    def test_warm_on_another_process_is_flagged(self):
        cold, warm, _ = self.probe("ping", {"warm_new_process": True})
        self.assertEqual(cold["anomalies"], [])
        self.assertEqual(len(warm["anomalies"]), 2)
        self.assertIn("different process", warm["anomalies"][0])


class DerivationTests(unittest.TestCase):
    def test_judge(self):
        self.assertEqual(ct.judge("ping", 200, {"mode": "ping"}), (True, None, None))
        self.assertEqual(ct.judge("ping", 502, {"mode": "ping"})[:2], (False, "http_error"))
        self.assertEqual(ct.judge("ping", 200, None)[:2], (False, "bad_body"))
        self.assertEqual(ct.judge("agent", 200, {"mode": "agent"})[:2], (False, "agent_failed"))
        self.assertEqual(ct.judge("agent", 200, {"agent": {"ok": "yes"}})[:2], (False, "agent_failed"))
        self.assertEqual(ct.judge("agent", 200, {"agent": {"ok": True}}), (True, None, None))

    def test_classify_boot(self):
        server = {"proc_start_ts": 100.0, "request_ts": 100.4, "response_ts": 103.4}
        self.assertEqual(ct.classify_boot(server, 9000.0), ct.BOOT_REAL)
        self.assertEqual(ct.classify_boot(server, 3200.0), ct.BOOT_POOL)
        del server["response_ts"]  # falls back to request_ts
        self.assertEqual(ct.classify_boot(server, 3200.0), ct.BOOT_REAL)
        self.assertEqual(ct.classify_boot({"request_ts": 1.0}, 50.0), ct.BOOT_UNKNOWN)
        self.assertEqual(ct.classify_boot({"proc_start_ts": True, "response_ts": 2.0}, 50.0), ct.BOOT_UNKNOWN)
        server = {"proc_start_ts": 100.0, "request_ts": 100.4, "response_ts": 103.4}
        self.assertEqual(ct.classify_boot(server, 3200.0, "V2"), ct.BOOT_SNAPSHOT)
        self.assertEqual(ct.classify_boot(server, 3200.0, "V1"), ct.BOOT_POOL)
        self.assertEqual(ct.classify_boot(server, 9000.0, "V2"), ct.BOOT_REAL)  # a V2 boot stays a boot

    def test_null_first_output_ts(self):
        rec = {"sequence": "agent", "attempted": True, "error_type": None, "status_code": 200,
               "total_ms": 900.0, "server": {"proc_start_ts": 1.0, "request_ts": 50.0, "response_ts": 50.8,
                                             "invocation_seq": 1,
                                             "agent": {"ok": True, "spawn_ts": 50.0, "first_output_ts": None,
                                                       "exit_ts": 50.7}}}
        ct.finalize(rec)
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["dsh_boot_ms"], rec["first_text_ms"], rec["dsh_total_ms"]), (None, None, 700.0))
        self.assertEqual(rec["platform_overhead_ms"], 200.0)

    def test_stats(self):
        self.assertEqual(ct.stats([100, 200, 300, 400, 1000, None]),
                         {"n": 5, "min": 100, "p50": 300, "p90": 760.0, "p99": 976.0,
                          "max": 1000, "mean": 400.0})
        self.assertEqual(ct.stats([None])["n"], 0)
        self.assertIsNone(ct.stats([])["p50"])
        self.assertEqual(ct.stats([42.0])["p99"], 42.0)

    def test_summarize_groups_by_boot_kind_and_counts_failures(self):
        def rec(phase, boot, total, ok=True, attempted=True, error=None):
            return {"runtime": "dsh", "sequence": "ping", "phase": phase, "concurrency": 1,
                    "boot_kind": boot, "attempted": attempted, "ok": ok, "error_type": error,
                    "anomalies": [], "total_ms": total, "headers_ms": total, "uptime_at_request_ms": 1.0,
                    "server_handling_ms": 1.0, "outside_handler_ms": total}
        cells = {(c["phase"], c["boot_kind"]): c for c in ct.summarize([
            rec("cold", "real_boot", 7000.0), rec("cold", "real_boot", 9000.0),
            rec("cold", "warm_pool", 400.0), rec("cold", "unknown", 50.0, ok=False, error="throttle"),
            rec("warm", "real_boot", 80.0), rec("warm", "real_boot", 90.0), rec("warm", "warm_pool", 70.0),
            rec("warm", "unknown", None, ok=False, attempted=False, error="skipped")])}
        self.assertEqual(list(cells), [("cold", "all"), ("cold", "real_boot"), ("cold", "warm_pool"),
                                       ("cold", "unknown"), ("warm", "all"), ("warm", "real_boot"),
                                       ("warm", "warm_pool"), ("warm", "unknown")])
        everything = cells[("cold", "all")]
        self.assertEqual((everything["n"], everything["ok"], everything["failures"], everything["skipped"]),
                         (4, 3, 1, 0))
        self.assertEqual(everything["error_types"], {"throttle": 1})
        self.assertEqual(everything["metrics"]["total_ms"]["p50"], 7000.0)  # failure excluded
        self.assertEqual(cells[("cold", "real_boot")]["metrics"]["total_ms"]["mean"], 8000.0)
        self.assertEqual(cells[("cold", "unknown")]["metrics"]["total_ms"]["n"], 0)
        skipped = cells[("warm", "unknown")]
        self.assertEqual((skipped["n"], skipped["failures"], skipped["skipped"]), (0, 0, 1))
        self.assertNotIn("dsh_total_ms", everything["metrics"])  # agent-only metric

    def test_default_rounds(self):
        self.assertEqual([ct.default_rounds(c) for c in (1, 5, 10, 50)], [10, 4, 2, 1])


class ProcessPlanTests(unittest.TestCase):
    def test_plan_processes(self):
        with patch.object(ct.os, "cpu_count", return_value=16):
            self.assertEqual(ct.plan_processes("auto", 1), ("spawn", 1))   # c=1: one spawned worker
            self.assertEqual(ct.plan_processes("auto", 5), ("spawn", 5))
            self.assertEqual(ct.plan_processes("auto", 200), ("spawn", 8))
        with patch.object(ct.os, "cpu_count", return_value=4):
            self.assertEqual(ct.plan_processes("auto", 30), ("spawn", 4))
        self.assertEqual(ct.plan_processes("1", 30), ("inline", 1))        # today's behaviour
        self.assertEqual(ct.plan_processes("16", 30), ("spawn", 16))
        self.assertEqual(ct.plan_processes("4", 2), ("spawn", 2))
        self.assertEqual(ct.parse_args(["--full"]).processes, "1")         # worker processes are opt-in
        for bad in ("0", "many", "-2"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                ct.parse_args(["--full", "--processes", bad])

    def test_connect_timer_wraps_once_and_charges_the_calling_request(self):
        clock = FakeClock()

        class Connection:
            def connect(self):
                clock.sleep(0.03)
                return "connected"

        with patch.object(ct, "time", clock):
            self.assertTrue(ct.install_connect_timer(Connection))
            wrapped = Connection.connect
            self.assertTrue(ct.install_connect_timer(Connection))  # second call changes nothing
            self.assertIs(Connection.connect, wrapped)
            self.assertEqual(Connection().connect(), "connected")  # outside a request: nothing to charge
            ct._MARKS.current = marks = {}
            Connection().connect()
            Connection().connect()
            ct._MARKS.current = None
        self.assertAlmostEqual(marks["connect"], 0.06)
        self.assertFalse(ct.install_connect_timer(object))


class SpawnTests(unittest.TestCase):
    """Real spawned worker processes with a fake transport; no AWS calls."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "results"
        self.dep = write_deployments(self.dir)

    def run_main(self, args, **factory_options):
        stdout, stderr = io.StringIO(), io.StringIO()
        factory = partial(fake_factory, default={"boot": "pool"}, **factory_options)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ct.main(["--full", "--runtimes", "dsh", "--sequences", "ping", *args, "--out", str(self.out),
                            "--deployments", str(self.dep)], client_factory=factory, sleep=lambda seconds: None)
        raws = {f.name.split("_", 1)[1]: json.loads(f.read_text()) for f in (self.out / "raw").glob("*.json")}
        return code, stdout.getvalue(), raws

    def test_workers_have_their_own_client_and_one_shared_release(self):
        code, stdout, raws = self.run_main(["--concurrency", "1,6", "--rounds", "2", "--processes", "3"],
                                           client_delay=1.0)
        self.assertEqual(code, 0, stdout)
        one, six = raws["dsh_ping_c1.json"], raws["dsh_ping_c6.json"]
        self.assertEqual((one["meta"]["process_mode"], one["meta"]["processes"], one["meta"]["threads_per_process"]),
                         ("spawn", 1, [1]))                       # c=1 still runs in a spawned worker
        self.assertEqual((six["meta"]["process_mode"], six["meta"]["processes"], six["meta"]["threads_per_process"],
                          six["meta"]["start_method"], six["meta"]["client_warmed"], six["meta"]["worker_failures"]),
                         ("spawn", 3, [2, 2, 2], "spawn", True, []))
        requests = six["requests"]
        self.assertEqual(len(requests), 24)                         # 6 probes x 2 rounds x cold+warm
        self.assertTrue(all(r["ok"] for r in requests))
        cold = [r for r in requests if r["phase"] == "cold"]
        self.assertEqual([(r["round"], r["probe_idx"]) for r in cold], [(n, i) for n in (1, 2) for i in range(6)])
        self.assertEqual({r["probe_idx"]: r["worker"] for r in cold}, {0: 0, 1: 1, 2: 2, 3: 0, 4: 1, 5: 2})
        pids = {r["worker"]: r["pid"] for r in cold}
        self.assertEqual(len(set(pids.values())), 3)                # three processes ...
        self.assertNotIn(os.getpid(), pids.values())                # ... none of them this one
        self.assertEqual({(r["worker"], r["pid"]) for r in cold}, set(pids.items()))  # reused in round 2
        self.assertEqual(len({r["session_id"] for r in cold}), 12)
        self.assertTrue(all(s["stopped"] for s in six["sessions"]))
        # every probe is timed from the same release; the fake spends 2 ms before before-send
        self.assertTrue(all(0 <= r["release_to_start_ms"] < 2000 for r in cold))
        self.assertTrue(all(r["start_to_send_ms"] >= 2.0 for r in requests))
        self.assertTrue(all(r["release_to_start_ms"] is None for r in requests if r["phase"] == "warm"))
        summary = json.loads((self.out / "summary.json").read_text())
        cell = next(c for c in summary["cells"] if (c["concurrency"], c["phase"], c["boot_kind"]) == (6, "cold", "all"))
        self.assertEqual((cell["process_mode"], cell["processes"], cell["threads_per_process"]), ("spawn", 3, [2, 2, 2]))
        self.assertEqual((cell["metrics"]["release_to_start_ms"]["n"], cell["metrics"]["start_to_send_ms"]["n"]),
                         (12, 12))
        self.assertEqual(summary["totals"]["worker_failures"], 0)
        self.assertIn("3 spawned process(es) x up to 2 thread(s)", stdout)

    def test_worker_that_cannot_build_its_client_does_not_stop_the_round(self):
        code, stdout, raws = self.run_main(["--concurrency", "4", "--rounds", "1", "--processes", "2"], fail_worker=1)
        self.assertEqual(code, 1)
        raw = raws["dsh_ping_c4.json"]
        cold = {r["probe_idx"]: r for r in raw["requests"] if r["phase"] == "cold"}
        self.assertEqual(len(raw["requests"]), 8)                   # nothing is missing from the round
        self.assertTrue(cold[0]["ok"] and cold[2]["ok"])            # worker 0 fired anyway
        for idx in (1, 3):
            self.assertEqual((cold[idx]["attempted"], cold[idx]["ok"], cold[idx]["error_type"], cold[idx]["worker"]),
                             (False, False, "not_sent", 1))
            self.assertIn("simulated client construction failure", cold[idx]["error_msg"])
        self.assertEqual([(f["worker"], f["phase"]) for f in raw["meta"]["worker_failures"]],
                         [(1, "start"), (1, "start")])              # at start-up and at the retry for round 1
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual((summary["totals"]["attempted"], summary["totals"]["ok"], summary["totals"]["skipped"],
                          summary["totals"]["worker_failures"]), (4, 4, 4, 2))
        self.assertIn("NOT SENT: 2", stdout)
        self.assertIn("worker failures: 2", stdout)

    def test_worker_that_dies_mid_request_is_recorded_and_replaced(self):
        code, stdout, raws = self.run_main(["--concurrency", "4", "--rounds", "2", "--processes", "2"],
                                           crash_worker=0, crash_once_file=str(self.dir / "crashed"))
        self.assertEqual(code, 1)
        raw = raws["dsh_ping_c4.json"]
        self.assertEqual(len(raw["requests"]), 16)
        first = {r["probe_idx"]: r for r in raw["requests"] if (r["round"], r["phase"]) == (1, "cold")}
        for idx in (0, 2):                                          # worker 0 died after the release
            self.assertEqual((first[idx]["attempted"], first[idx]["ok"], first[idx]["error_type"]),
                             (True, False, "worker_failed"))
            self.assertIn("exited with code 17", first[idx]["error_msg"])
            self.assertIn("may have been sent", first[idx]["error_msg"])
        self.assertTrue(first[1]["ok"] and first[3]["ok"])          # worker 1 was not affected
        lost = {first[0]["session_id"], first[2]["session_id"]}
        self.assertTrue(all(s["stopped"] for s in raw["sessions"] if s["session_id"] in lost))  # parent cleaned up
        second = [r for r in raw["requests"] if r["round"] == 2]
        self.assertTrue(all(r["ok"] for r in second))               # a fresh worker 0 served round 2
        self.assertNotEqual({r["pid"] for r in second if r["worker"] == 0},
                            {r["pid"] for r in first.values() if r["worker"] == 0})
        self.assertEqual([(f["round"], f["worker"], f["phase"]) for f in raw["meta"]["worker_failures"]],
                         [(1, 0, "after release")])
        summary = json.loads((self.out / "summary.json").read_text())
        cell = next(c for c in summary["cells"] if (c["phase"], c["boot_kind"]) == ("cold", "all"))
        self.assertEqual((cell["n"], cell["ok"], cell["failures"], cell["error_types"]),
                         (8, 6, 2, {"worker_failed": 2}))

    def pool(self, cls=ct.WorkerPool, concurrency=4, processes=2, **factory_options):
        spec = {"region": "us-east-1", "arn": ARN, "runtime": "dsh", "sequence": "ping", "prompt": "",
                "concurrency": concurrency, "platform_version": None, "read_timeout": 30,
                "gate_timeout": 30.0, "finish_timeout": 30.0}
        with contextlib.redirect_stdout(io.StringIO()):
            pool = cls(partial(fake_factory, default={"boot": "pool"}, **factory_options), spec, processes)
        self.addCleanup(pool.close)
        return pool

    def run_pool_round(self, pool, rnd):
        with contextlib.redirect_stdout(io.StringIO()):
            requests, sessions = pool.run_round(rnd)
        self.assertEqual([(r["probe_idx"], r["phase"]) for r in requests],
                         [(i, phase) for i in range(4) for phase in ("cold", "warm")])  # nothing missing
        return {r["probe_idx"]: r for r in requests if r["phase"] == "cold"}, sessions

    def test_worker_that_dies_between_rounds_is_noticed_and_replaced(self):
        pool = self.pool()
        pool.procs[1].kill()
        pool.procs[1].join()
        cold, _ = self.run_pool_round(pool, 1)
        self.assertTrue(cold[0]["ok"] and cold[2]["ok"])
        self.assertEqual({cold[i]["error_type"] for i in (1, 3)}, {"not_sent"})
        self.assertIn("exited with code -9", cold[1]["error_msg"])
        cold, _ = self.run_pool_round(pool, 2)
        self.assertTrue(all(r["ok"] for r in cold.values()))
        self.assertEqual([(f["round"], f["worker"], f["phase"]) for f in pool.failures],
                         [(1, 1, "before release")])

    def test_worker_killed_while_parked_at_the_gate_cannot_stall_the_release(self):
        class KilledAfterReady(ct.WorkerPool):
            def collect(self, kind, waiting, rnd, timeout, phase):
                got = super().collect(kind, waiting, rnd, timeout, phase)
                if (kind, rnd) == ("ready", 1):  # its threads are blocked on the gate right now
                    self.procs[0].kill()
                    self.procs[0].join()
                return got

        pool = self.pool(KilledAfterReady)
        cold, sessions = self.run_pool_round(pool, 1)   # a Barrier or Event would hang here
        self.assertTrue(cold[1]["ok"] and cold[3]["ok"])
        self.assertEqual({cold[i]["error_type"] for i in (0, 2)}, {"worker_failed"})
        self.assertTrue(all(s["stopped"] for s in sessions))
        cold, _ = self.run_pool_round(pool, 2)           # the two unused tokens were drained first
        self.assertTrue(all(r["ok"] and r["release_to_start_ms"] >= 0 for r in cold.values()))
        self.assertFalse(pool.gate.acquire(False))       # every token of round 2 was taken

    def test_worker_that_hangs_at_start_is_given_up_on(self):
        with contextlib.redirect_stdout(io.StringIO()):
            pool = ct.WorkerPool(partial(fake_factory, default={"boot": "pool"}, hang_worker=1),
                                 {"region": "us-east-1", "arn": ARN, "runtime": "dsh", "sequence": "ping",
                                  "prompt": "", "concurrency": 4, "platform_version": None, "read_timeout": 30,
                                  "gate_timeout": 30.0, "finish_timeout": 30.0}, 2, ready_timeout=1.5)
        self.addCleanup(pool.close)
        self.assertEqual(list(pool.dead), [1])
        self.assertIn("gave no 'started' within 1.5 s", pool.dead[1])
        self.assertFalse(pool.procs[1].is_alive())

    def test_script_entry_point_with_no_credentials_records_every_probe(self):
        """The real CLI, run as a script, with the real client factory and an
        environment that has no AWS credentials at all: every worker fails
        before the release, nothing is sent anywhere, nothing is lost."""
        home = self.dir / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "AWS_EC2_METADATA_DISABLED": "true",
               "AWS_CONFIG_FILE": str(home / "config"), "AWS_SHARED_CREDENTIALS_FILE": str(home / "credentials")}
        run = subprocess.run(
            [sys.executable, str(Path(ct.__file__)), "--full", "--runtimes", "dsh", "--sequences", "ping",
             "--concurrency", "3", "--rounds", "1", "--processes", "2", "--out", str(self.out),
             "--deployments", str(self.dep)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        raw = json.loads(next((self.out / "raw").glob("*.json")).read_text())
        self.assertEqual((raw["meta"]["process_mode"], raw["meta"]["processes"], raw["meta"]["threads_per_process"]),
                         ("spawn", 2, [2, 1]))
        cold = [r for r in raw["requests"] if r["phase"] == "cold"]
        self.assertEqual([(r["probe_idx"], r["attempted"], r["error_type"]) for r in cold],
                         [(0, False, "not_sent"), (1, False, "not_sent"), (2, False, "not_sent")])
        self.assertTrue(all("no AWS credentials found" in r["error_msg"] for r in cold))
        self.assertIn("worker failures: 4", run.stdout)  # 2 workers, at start-up and at the retry
        self.assertIn("NOT SENT: 3", run.stdout)

    def test_an_injected_client_cannot_be_used_with_worker_processes(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = ct.main(["--full", "--processes", "auto", "--out", str(self.out), "--deployments", str(self.dep)],
                           client=FakeRuntime())
        self.assertEqual(code, 2)
        self.assertIn("--processes 1", stderr.getvalue())
        self.assertFalse(self.out.exists())


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "results"
        self.dep = write_deployments(self.dir)

    def run_main(self, args, fake, sleeps=None):
        sleeps = [] if sleeps is None else sleeps
        stdout, stderr = io.StringIO(), io.StringIO()
        if "--full" in args and "--processes" not in args:
            args = [*args, "--processes", "1"]  # the injected fake lives in this process
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ct.main([*args, "--out", str(self.out), "--deployments", str(self.dep)],
                           client=fake, sleep=sleeps.append)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_full_matrix(self):
        fake, sleeps = FakeRuntime(), []
        code, stdout, _ = self.run_main(["--full", "--runtimes", "dsh", "--concurrency", "1,3", "--rounds", "2",
                                         "--settle-seconds", "7", "--prompt", "hello"], fake, sleeps)
        self.assertEqual(code, 0)
        raw_files = sorted((self.out / "raw").glob("*.json"))
        self.assertEqual(sorted(f.name.split("_", 1)[1] for f in raw_files),
                         ["dsh_agent_c1.json", "dsh_agent_c3.json", "dsh_ping_c1.json", "dsh_ping_c3.json"])
        raws = [json.loads(f.read_text()) for f in raw_files]
        requests = [r for raw in raws for r in raw["requests"]]
        sessions = [s for raw in raws for s in raw["sessions"]]
        self.assertEqual((len(requests), len(sessions)), (32, 16))  # 2 seq x (1 + 3) x 2 rounds x cold+warm
        self.assertTrue(all(r["ok"] for r in requests))
        self.assertEqual(len({s["session_id"] for s in sessions}), 16)  # a fresh session per probe
        self.assertTrue(all(s["stopped"] for s in sessions))
        self.assertEqual(len(fake.stops), 16)
        self.assertEqual({i["agentRuntimeArn"] for i in fake.invokes}, {ARN})  # --runtimes filter
        self.assertTrue(all(len(i["runtimeSessionId"]) >= 33 and i["qualifier"] == "DEFAULT"
                            for i in fake.invokes))
        self.assertEqual(fake.max_inflight, 3)  # the barrier released a c=3 round together
        self.assertEqual({r["boot_kind"] for r in requests}, {ct.BOOT_REAL})
        # settle pause: once between the 2 rounds of each of 4 cells, once between cells
        self.assertEqual(sleeps, [7.0] * 7)
        self.assertTrue(all(raw["meta"]["settle_seconds"] == 7.0 and raw["meta"]["rounds_completed"] == 2
                            for raw in raws))
        self.assertEqual({raw["meta"]["prompt"] for raw in raws}, {None, "hello"})

        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["settle_seconds"], [7.0])
        self.assertEqual(summary["sources"], [f.name for f in raw_files])
        self.assertEqual(summary["totals"], {"requests": 32, "attempted": 32, "ok": 32, "failures": 0,
                                             "skipped": 0, "anomalies": 0, "sessions": 16,
                                             "sessions_stopped": 16, "worker_failures": 0})
        cells = {(c["sequence"], c["phase"], c["concurrency"], c["boot_kind"]): c for c in summary["cells"]}
        self.assertEqual(len(cells), 16)  # 2 seq x 2 phases x 2 conc x (all + real_boot)
        # one process with threads: what --processes 1 has always meant
        self.assertTrue(all((raw["meta"]["process_mode"], raw["meta"]["processes"]) == ("inline", 1)
                            and raw["meta"]["client_warmed"] is False for raw in raws))
        self.assertEqual({tuple(raw["meta"]["threads_per_process"]) for raw in raws}, {(1,), (3,)})
        self.assertEqual({(r["pid"], r["worker"]) for r in requests}, {(os.getpid(), None)})
        cell = cells[("ping", "cold", 3, "all")]
        self.assertEqual((cell["process_mode"], cell["processes"], cell["threads_per_process"]), ("inline", 1, [3]))
        self.assertEqual(cell["metrics"]["release_to_start_ms"]["n"], 6)
        self.assertEqual(cells[("ping", "warm", 3, "all")]["metrics"]["release_to_start_ms"]["n"], 0)
        self.assertEqual(cells[("agent", "cold", 3, "real_boot")]["n"], 6)
        self.assertEqual(set(cells[("ping", "cold", 1, "all")]["metrics"]["total_ms"]),
                         {"n", "min", "p50", "p90", "p99", "max", "mean"})
        self.assertIn("dsh_total_ms", cells[("agent", "warm", 1, "all")]["metrics"])
        self.assertIn("failures: 0", stdout)

    def test_failures_are_counted_and_exit_code_is_nonzero(self):
        fake = FakeRuntime(plan=[{}, {"fail": "throttle"}, {"boot": "pool", "fail": "agent_fail"}])
        code, stdout, _ = self.run_main(["--full", "--runtimes", "dsh", "--rounds", "2"], fake)
        self.assertEqual(code, 1)
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual((summary["totals"]["failures"], summary["totals"]["skipped"]), (2, 1))
        cells = {(c["sequence"], c["phase"], c["boot_kind"]): c for c in summary["cells"]}
        self.assertEqual(cells[("ping", "cold", "all")]["error_types"], {"throttle": 1})
        self.assertEqual(cells[("ping", "cold", "unknown")]["failures"], 1)
        self.assertEqual(cells[("ping", "warm", "all")]["skipped"], 1)
        self.assertEqual(cells[("agent", "cold", "warm_pool")]["error_types"], {"agent_failed": 1})
        self.assertEqual(cells[("agent", "cold", "all")]["metrics"]["total_ms"]["n"], 1)
        self.assertIn("failures: 2", stdout)

    def test_platform_version_reaches_raw_files_and_summary(self):
        self.dep = write_deployments(self.dir, PLATFORM_DEPLOYMENTS)
        fake = FakeRuntime(plan=[{"boot": "pool"}, {"boot": "snapshot"}])
        code, stdout, _ = self.run_main(["--full", "--runtimes", "dsh,dsh_v2", "--sequences", "ping",
                                         "--rounds", "1"], fake)
        self.assertEqual(code, 0)
        raws = {f.name.split("_", 1)[1]: json.loads(f.read_text())
                for f in (self.out / "raw").glob("*.json")}
        self.assertEqual({name: raw["meta"]["platform_version"] for name, raw in raws.items()},
                         {"dsh_ping_c1.json": "V1", "dsh_v2_ping_c1.json": "V2"})
        self.assertEqual({r["boot_kind"] for r in raws["dsh_v2_ping_c1.json"]["requests"]}, {ct.BOOT_SNAPSHOT})
        self.assertEqual({r["boot_kind"] for r in raws["dsh_ping_c1.json"]["requests"]}, {ct.BOOT_POOL})
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(summary["platform_versions"], {"dsh": "V1", "dsh_v2": "V2"})
        self.assertEqual(sorted({(c["runtime"], c["boot_kind"]) for c in summary["cells"]}),
                         [("dsh", "all"), ("dsh", "warm_pool"), ("dsh_v2", "all"), ("dsh_v2", "snapshot_restore")])
        self.assertIn("snapshot_restore", stdout)

    def test_raw_files_from_before_the_label_are_relabelled_not_rewritten(self):
        # Written with a deployments.json that has no platform_version: boot kind is warm_pool.
        self.run_main(["--full", "--runtimes", "dsh", "--sequences", "ping", "--rounds", "2"],
                      FakeRuntime(plan=[{"boot": "snapshot"}, {"boot": "snapshot"}]))
        raw_file = next((self.out / "raw").glob("*.json"))
        before = raw_file.read_bytes()
        self.assertEqual({r["boot_kind"] for r in json.loads(before)["requests"]}, {ct.BOOT_POOL})
        summary = ct.rebuild_summary(self.out, "us-east-1", False, {"dsh": "V2"})
        self.assertEqual({c["boot_kind"] for c in summary["cells"]}, {"all", ct.BOOT_SNAPSHOT})
        self.assertEqual(summary["platform_versions"], {"dsh": "V2"})
        self.assertEqual(raw_file.read_bytes(), before)

    def test_latest_raw_file_wins(self):
        self.run_main(["--full", "--runtimes", "dsh", "--sequences", "ping", "--rounds", "3"], FakeRuntime())
        first = next((self.out / "raw").glob("*.json"))
        first.rename(first.with_name("20200101T000000Z_dsh_ping_c1.json"))
        self.run_main(["--full", "--runtimes", "dsh", "--sequences", "ping", "--rounds", "1"], FakeRuntime())
        summary = json.loads((self.out / "summary.json").read_text())
        self.assertEqual(len(summary["sources"]), 1)
        self.assertNotIn("20200101T000000Z_dsh_ping_c1.json", summary["sources"])
        self.assertEqual(summary["totals"]["requests"], 2)

    def test_smoke(self):
        fake = FakeRuntime()
        code, stdout, _ = self.run_main(["--smoke"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.sessions), 4)  # ping + agent on each of the two runtimes
        smoke = json.loads(next((self.out / "raw").glob("smoke_*.json")).read_text())
        self.assertEqual([(r["runtime"], r["sequence"], r["phase"]) for r in smoke["requests"][:4]],
                         [("dsh", "ping", "cold"), ("dsh", "ping", "warm"),
                          ("dsh", "agent", "cold"), ("dsh", "agent", "warm")])
        self.assertFalse((self.out / "summary.json").exists())  # smoke never feeds the summary
        for text in ("boot=real_boot", "dsh_total=", "platform_overhead=", "stopped: yes", "8/8 requests ok"):
            self.assertIn(text, stdout)

    def test_smoke_reports_failure(self):
        fake = FakeRuntime(plan=[{}, {"fail": "agent_fail"}])
        code, stdout, _ = self.run_main(["--smoke", "--runtimes", "dsh"], fake)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  agent_failed status=200", stdout)
        self.assertIn("3/4 requests ok", stdout)

    def test_bad_inputs(self):
        code, _, stderr = self.run_main(["--smoke", "--runtimes", "nope"], FakeRuntime())
        self.assertEqual(code, 2)
        self.assertIn("nope", stderr)
        self.dep.unlink()
        code, _, stderr = self.run_main(["--smoke"], FakeRuntime())
        self.assertEqual(code, 2)
        self.assertIn("deployments.json not found", stderr)
        for bad in (["--full", "--sequences", "pong"], ["--full", "--concurrency", "0"], ["--full", "--smoke"]):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                ct.parse_args(bad)


class BotoContractTests(unittest.TestCase):
    """The fake accepts any keyword, so check the real ones against botocore's
    bundled service model. Nothing here creates a client or sends a request."""

    def test_retries_are_off(self):
        with patch.object(ct.boto3, "client") as factory:
            ct.make_client("us-east-1", 50, read_timeout=120)
        (service,), kwargs = factory.call_args
        self.assertEqual((service, kwargs["region_name"]), ("bedrock-agentcore", "us-east-1"))
        config = kwargs["config"]
        self.assertEqual(config.retries, {"total_max_attempts": 1})
        self.assertEqual((config.read_timeout, config.max_pool_connections), (120, 100))

    def test_warm_client_resolves_credentials_before_it_is_returned(self):
        with patch.object(ct.boto3, "Session") as session_class:
            session = session_class.return_value
            client = ct.make_warm_client("us-east-1", 8, read_timeout=120, worker=3)
        session_class.assert_called_once_with(region_name="us-east-1")
        session.get_credentials.return_value.get_frozen_credentials.assert_called_once_with()
        (service,), kwargs = session.client.call_args
        self.assertEqual(service, "bedrock-agentcore")
        self.assertEqual(kwargs["config"].retries, {"total_max_attempts": 1})
        self.assertEqual((kwargs["config"].read_timeout, kwargs["config"].max_pool_connections), (120, 10))
        self.assertIs(client, session.client.return_value)
        with patch.object(ct.boto3, "Session") as session_class:
            session_class.return_value.get_credentials.return_value = None
            with self.assertRaisesRegex(RuntimeError, "no AWS credentials"):
                ct.make_warm_client("us-east-1", 8)

    def test_hooks_fire_inside_real_botocore(self):
        """A real client aimed at a closed loopback port: the request is built and
        signed by botocore, the connection is refused, nothing leaves this host."""
        proxies = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy")}
        with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            for name in proxies:
                if name.lower() != "no_proxy":
                    os.environ.pop(name, None)
            client = boto3.client(
                "bedrock-agentcore", region_name="us-east-1", endpoint_url="https://127.0.0.1:1",
                aws_access_key_id="testing", aws_secret_access_key="testing",
                config=Config(retries={"total_max_attempts": 1}, connect_timeout=2, read_timeout=2))
            self.assertTrue(ct.instrument(client))
            self.assertTrue(ct.instrument(client))        # registering twice must not double anything
            self.assertTrue(ct.install_connect_timer())
            call = ct.invoke_once(client, ARN, "s" * 40, {"mode": "ping"})
        self.assertEqual(call["error_type"], "other")
        self.assertIn("127.0.0.1", call["error_msg"])
        self.assertIsNotNone(call["start_to_send_ms"])    # before-send fired
        self.assertIsNotNone(call["connect_ms"])          # the connection attempt was timed
        self.assertGreaterEqual(call["total_ms"], call["start_to_send_ms"] + call["connect_ms"] - 0.2)
        self.assertIsNone(ct._MARKS.current)

    def test_request_parameters_match_the_service_model(self):
        fake = FakeRuntime(FakeClock())
        with patch.object(ct, "time", fake.clock):
            ct.probe(fake, "dsh", ARN, "agent", "say pong", 1, 1, 0, None)
        model = botocore.session.get_session().get_service_model("bedrock-agentcore")
        for operation, calls in (("InvokeAgentRuntime", fake.invokes), ("StopRuntimeSession", fake.stops)):
            shape = model.operation_model(operation).input_shape
            for params in calls:
                report = ParamValidator().validate(params, shape)
                self.assertFalse(report.has_errors(), report.generate_report())


if __name__ == "__main__":
    unittest.main()
