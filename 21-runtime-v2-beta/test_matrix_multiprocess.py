"""Offline matrix scope, image metadata and real-spawn low-concurrency tests."""
import json
from pathlib import Path
import tempfile
import unittest

import matrix_multiprocess_client as client
import coldstart_v2_matrix as matrix
from test_multiprocess_coldstart import fake_factory, failed_factory

class MatrixTests(unittest.TestCase):
    def test_plan_exact_missing_and_reuse(self):
        cells = matrix.cell_plan(matrix.HERE / "results/test-not-created")
        self.assertEqual(len(cells), 15)
        reused = [c for c in cells.values() if c["reused"]]
        missing = [c for c in cells.values() if not c["reused"]]
        self.assertEqual(len(reused), 3)
        self.assertEqual(len(missing), 12)
        self.assertEqual(sum(c["concurrency"] for c in missing), 733)
        self.assertEqual(sum(c["concurrency"] for c in cells.values()), 1083)
        self.assertEqual({(c["size"], c["concurrency"]) for c in reused},
                         {("500mb", 50), ("500mb", 100), ("500mb", 200)})

    def test_spawn_low_concurrency_and_image_labels(self):
        for size, concurrency in [("500mb", 1), ("1gb", 10), ("2gb", 50)]:
            with self.subTest(size=size, concurrency=concurrency), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                summary = client.run_burst(size, "region", "arn", concurrency, out, factory=fake_factory)
                self.assertEqual(summary["size"], size)
                self.assertEqual(summary["success"], concurrency)
                raw = json.loads((out / "raw.json").read_text())
                self.assertEqual(len(raw["requests"]), concurrency)
                self.assertEqual({r["size"] for r in raw["requests"]}, {size})
                result = json.loads((out / "client_result.json").read_text())
                self.assertEqual(len({r["pid"] for r in result["reports"]}), min(8, concurrency))
                self.assertTrue(all(r["indices"] for r in result["reports"]))
                self.assertEqual(sum(r["pool_size"] for r in result["reports"]), 2 * concurrency)
                self.assertEqual({r["size"] for p in result["reports"] for r in p["rows"]}, {size})
                self.assertTrue(all(e["started_perf"] >= result["release_perf"] for e in raw["events"]))

    def test_initialization_failure_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(RuntimeError):
                client.run_burst("2gb", "region", "arn", 1, out, factory=failed_factory,
                                 ready_timeout=5, finish_timeout=5)
            result = json.loads((out / "client_result.json").read_text())
            self.assertEqual(result["release_perf"], 0)
            self.assertIsNotNone(result["failure"])

    def test_reused_cleanup_is_never_called(self):
        from unittest.mock import MagicMock
        ctl = MagicMock()
        state = {"cells": {"reuse": {"reused": True, "runtime": {"id": "do-not-delete"}}}}
        matrix.cleanup_cell(ctl, state, "reuse", Path("unused"))
        ctl.delete_agent_runtime.assert_not_called()



if __name__ == "__main__":
    unittest.main()
