"""Offline tests for the MicroVM-specific spans and clock-offset bounds (no AWS calls).

    ../../.venv/bin/python -B -m unittest test_bench_microvm -v
"""

import unittest

import bench_microvm as bm

MS = 1_000_000
BASE = 1_800_000_000_000 * MS


def t(ms: float) -> int:
    return BASE + int(ms * MS)


def warm(send, first_byte, received):
    return {"client": {"send_ns": t(send), "first_byte_ns": t(first_byte)},
            "done": {"probes": {"request_received_ns": t(received)}}}


class ClockOffsetTest(unittest.TestCase):
    def test_bounds_intersect_across_turns(self):
        # VM clock 80 ms ahead: received shows up 80 ms "late" on the client axis.
        off = bm.clock_offset_ms([warm(0, 10, 83), warm(1000, 1006, 1084)])
        self.assertAlmostEqual(off[0], 78)
        self.assertAlmostEqual(off[1], 83)

    def test_inconsistent_or_missing_turns(self):
        self.assertIsNone(bm.clock_offset_ms([]))
        self.assertIsNone(bm.clock_offset_ms([warm(0, 5, 20), warm(100, 140, 100)]))


class MicrovmStagesTest(unittest.TestCase):
    def test_cold_spans_and_correction(self):
        rec = {"run_api_ms": 120.0, "token_ms": 150.0,
               "client": {"send_ns": t(0)},
               "attempt_log": [{"send_ns": t(300), "end_ns": t(3300), "status": 502},
                               {"send_ns": t(3400), "end_ns": t(9000), "status": 200}],
               "done": {"server": {"run_hook_ns": t(1680)}, "probes": {"request_received_ns": t(3500)}}}
        x = bm.microvm_stages(rec, (70.0, 90.0))
        self.assertAlmostEqual(x["send_to_run_hook"], 1680)
        self.assertAlmostEqual(x["run_hook_to_received"], 1820)
        self.assertAlmostEqual(x["ok_attempt_send_to_received"], 100)
        self.assertEqual(x["attempts"], 2)
        self.assertAlmostEqual(x["failed_attempts_ms"], 3000)
        self.assertAlmostEqual(x["send_to_run_hook_corrected"], 1600)
        self.assertAlmostEqual(x["send_to_received_corrected"], 3420)
        self.assertNotIn("send_to_received_corrected", bm.microvm_stages(rec))


if __name__ == "__main__":
    unittest.main()
