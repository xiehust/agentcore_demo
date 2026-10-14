"""Offline tests for the stage arithmetic and SSE parsing (no AWS calls).

    ../.venv/bin/python -B -m unittest test_stages -v
"""

import unittest

import stages
from bench import parse_sse

MS = 1_000_000


def record(*, proc_start, connect_start, turn=1, error=None):
    """A synthetic record: client sends at t=0 (ms), all other times relative."""
    base = 1_800_000_000_000 * MS
    t = lambda ms: base + int(ms * MS)  # noqa: E731
    return {
        "turn": turn,
        "error": None,
        "client": {"send_ns": t(0), "headers_ns": t(1500), "first_byte_ns": t(1510),
                   "first_token_ns": t(4000), "done_ns": t(4300)},
        "done": {
            "error": error,
            "boot": {"wall_minus_mono_ns": 7 * MS, "vm": {"cached_kb": 900 * 1024, "pgmajfault": 10},
                     "kernel_boot_ns": t(proc_start - 300), "proc_start_ns": t(proc_start),
                     "module_ns": t(proc_start + 100), "stdlib_ns": t(proc_start + 150),
                     "sdk_import_ns": t(proc_start + 800), "imports_ns": t(proc_start + 900)},
            "server": {"listening_ns": t(proc_start + 920), "first_ping_ns": t(proc_start + 950)},
            "agent": {"connect_start_ns": t(connect_start), "connect_done_ns": t(connect_start + 2000),
                      "vm_before": {"cached_kb": 200 * 1024, "pgmajfault": 100},
                      "vm_after": {"cached_kb": 340 * 1024, "pgmajfault": 2100}},
            "probes": {"wall_minus_mono_ns": 90_007 * MS, "vm": {"cached_kb": 200 * 1024},
                       "request_received_ns": t(1490), "turn_lock_ns": t(1491),
                       "agent_ready_ns": t(max(1491, connect_start + 2000)),
                       "query_sent_ns": t(3500), "message_start_ns": t(3990),
                       "first_token_ns": t(3995), "result_ns": t(4200), "done_sent_ns": t(4210)},
            "result": {"duration_ms": 700, "duration_api_ms": 650},
        },
    }


class ComputeTest(unittest.TestCase):
    def test_lazy_cold_boot_puts_everything_in_the_request(self):
        out = stages.compute(record(proc_start=200, connect_start=1492))
        s, f = out["stages"], out["flags"]
        self.assertTrue(f["ok"] and f["booted_by_request"] and f["agent_in_request"] and f["clock_order_ok"])
        self.assertAlmostEqual(s["send_to_kernel_boot"], -100)
        self.assertAlmostEqual(s["kernel_to_proc"], 300)
        self.assertAlmostEqual(s["sdk_import"], 650)
        self.assertAlmostEqual(s["proc_to_listening"], 920)
        self.assertAlmostEqual(s["send_to_listening"], 1120)
        self.assertAlmostEqual(s["agent_connect"], 2000)
        self.assertAlmostEqual(s["agent_wait"], 2001)
        self.assertAlmostEqual(s["server_ttft"], 495)
        self.assertAlmostEqual(s["client_total"], 4300)
        self.assertAlmostEqual(s["frozen_gap"], 90_000)
        self.assertAlmostEqual(s["cache_drop_mb"], 700)
        self.assertAlmostEqual(s["agent_connect_read_mb"], 140)
        self.assertEqual(s["agent_connect_majfault"], 2000)

    def test_prewarmed_eager_agent_is_outside_the_request(self):
        out = stages.compute(record(proc_start=-60_000, connect_start=-59_000))
        s, f = out["stages"], out["flags"]
        self.assertFalse(f["booted_by_request"] or f["agent_in_request"])
        self.assertIsNone(s["send_to_kernel_boot"])
        self.assertIsNone(s["send_to_listening"])
        self.assertIsNone(s["agent_connect"])
        self.assertAlmostEqual(s["agent_connect_outside_request"], 2000)
        self.assertAlmostEqual(s["agent_wait"], 0)
        self.assertAlmostEqual(s["container_age_at_request"], 61_490)
        self.assertIsNone(s["agent_connect_read_mb"])

    def test_boot_margin(self):
        inside = record(proc_start=-(stages.BOOT_MARGIN_NS // MS) + 1, connect_start=1492)
        outside = record(proc_start=-(stages.BOOT_MARGIN_NS // MS) - 1, connect_start=1492)
        self.assertTrue(stages.compute(inside)["flags"]["booted_by_request"])
        self.assertFalse(stages.compute(outside)["flags"]["booted_by_request"])

    def test_clock_order_violation_and_errors(self):
        rec = record(proc_start=200, connect_start=1492)
        rec["done"]["probes"]["request_received_ns"] = rec["client"]["send_ns"] - MS
        self.assertFalse(stages.compute(rec)["flags"]["clock_order_ok"])
        self.assertFalse(stages.compute(record(proc_start=200, connect_start=1492, error="x"))["flags"]["ok"])
        failed = {"turn": 1, "error": "ThrottlingException: x", "client": {"send_ns": 1, "done_ns": 2}, "done": None}
        self.assertFalse(stages.compute(failed)["flags"]["ok"])

    def test_summarize(self):
        self.assertIsNone(stages.summarize([None]))
        self.assertEqual(stages.summarize([5.0])["p90"], 5.0)
        stat = stages.summarize([1, 2, 3, 4, None, 5])
        self.assertEqual((stat["n"], stat["p50"], stat["p90"], stat["max"]), (5, 3, 4.6, 5))


class SseTest(unittest.TestCase):
    def test_events_split_across_chunks(self):
        events, rest = parse_sse(b'data: {"event": "accepted"}\n\ndata: {"event": "de')
        self.assertEqual(events, [{"event": "accepted"}])
        events, rest = parse_sse(rest + b'lta", "text": "hi"}\n\n')
        self.assertEqual((events, rest), ([{"event": "delta", "text": "hi"}], b""))


if __name__ == "__main__":
    unittest.main()
