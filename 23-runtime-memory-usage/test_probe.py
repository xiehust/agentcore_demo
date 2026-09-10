"""Local tests: real resident pages, HTTP contract, and input bounds."""
import contextlib
import io
import json
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import probe


class ProbeTests(unittest.TestCase):
    def test_kib_parser(self):
        self.assertEqual(probe.parse_kib("Name:\tpython\nVmRSS:\t42 kB\nThreads:\t2\n"),
                         {"VmRSS": 42 * 1024})

    def test_bad_inputs(self):
        for payload in [{"mib": 10000}, {"mib": True}, {"phase_seconds": 100},
                        {"phase_seconds": float("nan")}, {"kind": "shell"}]:
            with self.assertRaises(ValueError):
                probe.experiment(payload)

    def test_anonymous_allocation_is_resident_and_released(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = probe.experiment({"kind": "anonymous", "mib": 32, "phase_seconds": 0.1})
        rss = {p: max(s["smaps_rollup"]["Rss"] for s in result["samples"] if s["phase"] == p)
               for p in ["baseline", "allocated", "released"]}
        self.assertGreater(rss["allocated"] - rss["baseline"], 30 * probe.MIB)
        self.assertGreater(rss["allocated"] - rss["released"], 30 * probe.MIB)
        self.assertTrue(result["samples"][0]["guest_meminfo"]["MemTotal"] > 0)

    def test_file_cache_does_not_load_whole_file_into_process(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = probe.experiment({"kind": "file_cache", "mib": 32, "phase_seconds": 0.1})
        rss = [s["smaps_rollup"]["Rss"] for s in result["samples"]]
        self.assertLess(max(rss) - min(rss), 16 * probe.MIB)
        self.assertEqual([p["phase"] for p in result["phases"]],
                         ["baseline", "file_cached", "after_fadvise", "file_closed"])

    def test_http_contract(self):
        server = probe.ThreadingHTTPServer(("127.0.0.1", 0), probe.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/ping") as response:
                self.assertEqual(json.load(response), {"status": "Healthy"})
            probe.BUSY.acquire()
            try:
                with urlopen(base + "/ping") as response:
                    self.assertEqual(json.load(response), {"status": "HealthyBusy"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base + "/invocations", data=b'{}'))
                self.assertEqual(caught.exception.code, 409)
            finally:
                probe.BUSY.release()
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base + "/invocations", data=b'[]'))
            self.assertEqual(caught.exception.code, 400)
            with contextlib.redirect_stdout(io.StringIO()):
                with urlopen(Request(base + "/invocations", data=json.dumps(
                        {"kind": "baseline", "phase_seconds": 0.1}).encode())) as response:
                    self.assertEqual(len(json.load(response)["phases"]), 2)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
