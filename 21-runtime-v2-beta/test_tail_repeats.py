"""No-cloud scope, failure handling and tail-statistic tests."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import repeat_tail_latency as runner

class TailRepeatTests(unittest.TestCase):
    def test_six_cells_alternating_exact_scope(self):
        cells = runner.planned_cells(runner.HERE / "results/not-created")
        self.assertEqual(len(cells), 6)
        self.assertEqual([(c["size"], c["concurrency"], c["repeat"]) for c in cells.values()],
                         [(s, c, r) for r in (1, 2, 3) for s, c in runner.CASES])
        self.assertEqual(sum(c["concurrency"] for c in cells.values()), 900)
        self.assertFalse(any(c["reused"] for c in cells.values()))

    def test_tail_thresholds_and_empty_singleton(self):
        from verify_tail_repeats import tail_stats
        self.assertIsNone(tail_stats([])["p99_ms"])
        self.assertIsNone(tail_stats([4500])["p99_ms"])
        s = tail_stats([1000, 4000, 4000.01, 5000, 5000.01, 6000])
        self.assertEqual(s["over_4s"], 4)
        self.assertEqual(s["over_5s"], 2)
        self.assertEqual(s["p99_ms"], 5950.001)

    def test_baseline_full_precision_tail_counts(self):
        from verify_tail_repeats import analyze_cell
        state = json.loads((runner.ORIGINAL / "matrix.json").read_text())
        for key, count4, count5 in [("500mb_c100", 4, 3), ("2gb_c200", 1, 1)]:
            stats, _, _ = analyze_cell(state["cells"][key])
            self.assertEqual(stats["over_4s"], count4)
            self.assertEqual(stats["over_5s"], count5)
            self.assertTrue(stats["tail_requests"])

    def test_no_cloud_orchestration_and_failure_cleanup(self):
        for fail in (False, True):
            with self.subTest(fail=fail), TemporaryDirectory() as tmp, patch.object(runner, "HERE", Path(tmp)):
                out = Path(tmp) / "output"
                out.mkdir()
                cells = runner.planned_cells(out)
                state = {"cells": cells, "region": "us-west-2", "requests": {k: {} for k in cells},
                         "images": {s: {"imageDigest": "digest"} for s, _ in runner.CASES}}
                ctl = MagicMock()
                ctl.create_agent_runtime.side_effect = [
                    {"agentRuntimeId": f"id{i}", "agentRuntimeArn": f"arn{i}"} for i in range(6)]
                clock = [1000.0]
                waits, bursts = [], []
                def sleep(seconds):
                    waits.append(seconds)
                    clock[0] += seconds
                def burst(size, region, arn, concurrency, folder):
                    bursts.append((size, concurrency, clock[0]))
                    if fail:
                        raise RuntimeError("test failure")
                    clock[0] += 5
                    return {"samples": concurrency, "success": concurrency, "warm_success": concurrency,
                            "stop_success": concurrency}
                def cleanup(control, data, key, directory):
                    data["cells"][key]["runtime"]["deleted"] = True
                base = runner.matrix.client.base
                with patch.object(runner, "preflight", return_value=(state, ctl)), \
                        patch.object(runner.time, "perf_counter", side_effect=lambda: clock[0]), \
                        patch.object(runner.time, "sleep", side_effect=sleep), \
                        patch.object(base.shared.original, "wait_ready", return_value={"platformVersion": "V2"}), \
                        patch.object(base.shared.original, "wait_endpoint_ready"), \
                        patch.object(base, "make_client", return_value=MagicMock()), \
                        patch.object(base, "Recorder", return_value=MagicMock(events=[])), \
                        patch.object(base, "probe", side_effect=lambda *a: {"success": True, "cold_ms": 2, "warm_ms": 1, "stopped": True}), \
                        patch.object(runner.matrix.client, "run_burst", side_effect=burst), \
                        patch.object(runner.matrix, "cleanup_cell", side_effect=cleanup) as cleaned, \
                        patch.object(runner.matrix.previous, "stop_unresolved") as recovery:
                    self.assertEqual(runner.run(out), 1 if fail else 0)
                self.assertEqual(ctl.create_agent_runtime.call_count, 6)
                self.assertTrue(state["cleanup_complete"])
                self.assertEqual(set(c.args[2] for c in cleaned.call_args_list), set(cells))
                self.assertEqual(len(bursts), 1 if fail else 6)
                self.assertTrue(all(t >= 180 for t in waits))
                for earlier, later in zip(bursts, bursts[1:]):
                    self.assertGreaterEqual(later[2] - earlier[2], 185)
                self.assertEqual(recovery.call_count, int(fail))



if __name__ == "__main__":
    unittest.main()
