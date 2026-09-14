"""Offline safety and exact A/B workload tests; no AWS calls."""
import asyncio
from collections import Counter
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import hyst
import run_v2
from botocore.exceptions import ClientError

class Socket:
    def __init__(self, sid, available=7000):
        self.sid = sid
        self.available = available
        self.messages = []
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send(self, raw):
        msg = json.loads(raw)
        self.commands.append(msg)
        action = msg["action"]
        if action == "ping":
            self.messages = [{"action": "pong", "session_id": self.sid,
                "memstat": {"uptime_s": 2, "vm_mem_available_mb": self.available,
                            "cgroup_mem_max": "max"}}]
        elif action == "memwatch":
            self.messages = [{"action": "memsample", "session_id": self.sid,
                              "seq": i, "ts": i} for i in range(msg["duration_s"])]
            self.messages.append({"action": "memwatch_done"})
        else:
            self.messages = [{"action": action + "_result", "held_mb": 4096 if action == "alloc" else 0}]

    async def recv(self):
        return json.dumps(self.messages.pop(0))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, arm, available=7000):
        sid = "hyst" + arm + "-" + "a" * 40
        sock = Socket(sid, available)
        c = MagicMock()
        c.generate_ws_connection.return_value = ("wss://example.invalid", {})
        samples, record = [], {"sid": sid, "events": []}
        args = SimpleNamespace(arn="target", endpoint="DEFAULT")
        with patch.object(hyst.websockets, "connect", return_value=sock):
            await hyst.run(arm, c, samples, record, args, lambda: None, io.StringIO())
        c.generate_ws_connection.assert_called_once_with(runtime_arn="target", session_id=sid, endpoint_name="DEFAULT")
        return sock, samples, record

    async def test_original_b_phases(self):
        sock, samples, record = await self.exercise("B")
        self.assertEqual(Counter(s["_phase"] for s in samples), {"pre": 60, "held": 60, "tail": 240})
        self.assertEqual([x["action"] for x in sock.commands], ["ping", "memwatch", "alloc", "memwatch", "free", "memwatch"])
        self.assertEqual(sock.commands[2]["mb"], 4096)
        self.assertEqual([x[2] for x in record["events"]][1:], [4096, 0])

    async def test_a_never_allocates(self):
        sock, samples, _ = await self.exercise("A")
        self.assertEqual(Counter(s["_phase"] for s in samples), {"pre": 60, "held_noop": 60, "tail": 240})
        self.assertNotIn("alloc", [x["action"] for x in sock.commands])

    async def test_wrong_allocation_is_rejected(self):
        original = Socket.recv
        async def wrong(sock):
            r = json.loads(await original(sock))
            if r.get("action") == "alloc_result": r["held_mb"] = 0
            return json.dumps(r)
        with patch.object(Socket, "recv", wrong):
            with self.assertRaisesRegex(ValueError, "Allocation not confirmed"):
                await self.exercise("B")

    async def test_insufficient_memory_aborts(self):
        with self.assertRaisesRegex(ValueError, "Insufficient available"):
            await self.exercise("B", 4000)


class CleanupTests(unittest.TestCase):
    def test_stop_and_delete_confirmed(self):
        ctl, data = MagicMock(), MagicMock()
        ctl.delete_agent_runtime.return_value = {"status": "DELETING"}
        ctl.get_agent_runtime.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
        data.stop_runtime_session.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
        with TemporaryDirectory() as d:
            out = Path(d)
            (out / "events.json").write_text(json.dumps([{"sid": "a"}, {"sid": "b"}]))
            state = {"runtime_id": "owned", "runtime_arn": "arn"}
            with patch.object(run_v2, "client", side_effect=lambda name: ctl if name.endswith("control") else data):
                run_v2.cleanup(out, state)
            self.assertTrue(state["runtime_deleted"])
            self.assertEqual(data.stop_runtime_session.call_count, 2)
            ctl.delete_agent_runtime.assert_called_once_with(agentRuntimeId="owned")

    def test_transport_stop_failure_still_deletes(self):
        ctl, data = MagicMock(), MagicMock()
        ctl.delete_agent_runtime.return_value = {}
        ctl.get_agent_runtime.side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetAgentRuntime")
        data.stop_runtime_session.side_effect = ConnectionError("transport")
        with TemporaryDirectory() as d:
            out = Path(d)
            (out / "events.json").write_text('[{"sid":"a"}]')
            state = {"runtime_id": "owned", "runtime_arn": "arn"}
            with patch.object(run_v2, "client", side_effect=lambda n: ctl if n.endswith("control") else data):
                run_v2.cleanup(out, state)
            self.assertTrue(state["runtime_deleted"])
            self.assertIn("error", state["stops"]["a"])

    def test_non_v2_is_rejected(self):
        ctl = MagicMock()
        ctl.get_agent_runtime.return_value = {"status": "READY", "platformVersion": "V1"}
        ctl.get_agent_runtime_endpoint.return_value = {"status": "READY"}
        with self.assertRaises(AssertionError):
            run_v2.wait_ready(ctl, "owned")


class UsageTests(unittest.TestCase):
    def test_normalize_strict_runtime_and_interval(self):
        from verify_v2 import normalize
        record = {"resource_arn": "arn", "event_timestamp": "1700000000000",
                  "attributes": {"session.id": "sid", "time_elapsed_seconds": 2},
                  "metrics": {"agent.runtime.memory.gb_hours.used": 4 / 3600}}
        event = {"eventId": "id", "message": json.dumps(record)}
        result = normalize(event, "arn", {"sid"})
        self.assertEqual(result["ts"], 1700000000)
        self.assertEqual(result["gb"], 2)
        self.assertIsNone(normalize(event, "other", {"sid"}))
        record["attributes"]["time_elapsed_seconds"] = 0
        event["message"] = json.dumps(record)
        with self.assertRaises(AssertionError): normalize(event, "arn", {"sid"})

    def test_conflicting_identity_rejected(self):
        from verify_v2 import normalize
        record = {"resource_arn": "wrong", "attributes": {"resource_arn": "arn", "session.id": "sid"}, "metrics": {}}
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            normalize({"eventId": "id", "message": json.dumps(record)}, "arn", {"sid"})

    def test_duration_weighted_mean_and_boundary_exclusion(self):
        from verify_v2 import phase_stats
        rows = [{"ts": 0, "seconds": 1, "gb_hours": 50 / 3600, "gb": 50},
                {"ts": 1, "seconds": 1, "gb_hours": 2 / 3600, "gb": 2},
                {"ts": 2, "seconds": 2, "gb_hours": 8 / 3600, "gb": 4}]
        stats = phase_stats(rows, 1, 2)
        self.assertEqual(stats["count"], 2)
        self.assertAlmostEqual(stats["mean_gb"], 10 / 3)


class AnalysisTests(unittest.TestCase):
    def fixture(self, out):
        samples, events, records = [], [], []
        start = 1700000000
        for arm in "AB":
            sid = "sid-" + arm
            records.append({"arm": arm, "sid": sid,
                "events": [["connect", start, 2]] + ([["alloc", start + 60, 4096], ["free", start + 120, 0]] if arm == "B" else []),
                "responses": [{"process_start_ts": start - 2}] + ([{"held_mb": 4096, "ts": start + 60}, {"held_mb": 0, "ts": start + 120}] if arm == "B" else [])})
            for i in range(360):
                phase = "pre" if i < 60 else ("held" if arm == "B" else "held_noop") if i < 120 else "tail"
                seq = i if i < 60 else i - 60 if i < 120 else i - 120
                rss = 4200 if phase == "held" else 100
                samples.append({"session_id": sid, "_session_id": sid, "_arm": arm,
                    "_phase": phase, "seq": seq, "ts": start + i, "uptime_s": i + 2,
                    "proc_rss_mb": rss, "vm_mem_used_mb": rss + 100,
                    "vm_mem_cached_mb": 50, "vm_mem_free_mb": 8000 - rss})
                gb = 5 if phase == "held" else 1
                events.append({"eventId": f"{arm}-{i}", "message": json.dumps({
                    "resource_arn": "arn", "event_timestamp": (start + i) * 1000,
                    "attributes": {"session.id": sid, "time_elapsed_seconds": 1},
                    "metrics": {"agent.runtime.memory.gb_hours.used": gb / 3600}})})
        (out / "state.json").write_text(json.dumps({"runtime_arn": "arn", "runtime": {"platformVersion": "V2"}}))
        (out / "events.json").write_text(json.dumps(records))
        (out / "memory_samples.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
        return events

    def test_full_recovery_and_complete_coverage(self):
        from verify_v2 import analyze
        with TemporaryDirectory() as d:
            out = Path(d)
            result = analyze(out, self.fixture(out))
            self.assertTrue(result["complete"])
            self.assertAlmostEqual(result["arms"]["B"]["fraction_excess_recovered"], 1)
            self.assertEqual(result["matched_records"], 720)

    def test_partial_tail_is_not_complete(self):
        from verify_v2 import analyze
        with TemporaryDirectory() as d:
            out = Path(d)
            events = self.fixture(out)
            result = analyze(out, events[:-100])
            self.assertFalse(result["complete"])

    def test_half_duration_coverage_rejected(self):
        from verify_v2 import analyze
        with TemporaryDirectory() as d:
            out = Path(d)
            events = self.fixture(out)
            for e in events:
                r = json.loads(e["message"])
                r["attributes"]["time_elapsed_seconds"] = .5
                e["message"] = json.dumps(r)
            self.assertFalse(analyze(out, events)["complete"])

    def test_control_treatment_rejected(self):
        from verify_v2 import analyze
        with TemporaryDirectory() as d:
            out = Path(d)
            events = self.fixture(out)
            records = json.loads((out / "events.json").read_text())
            records[0]["events"].append(["alloc", 1700000060, 4096])
            (out / "events.json").write_text(json.dumps(records))
            with self.assertRaisesRegex(AssertionError, "Unexpected arm commands"):
                analyze(out, events)

    def test_duplicate_interval_rejected(self):
        from verify_v2 import analyze
        with TemporaryDirectory() as d:
            out = Path(d)
            events = self.fixture(out)
            events.append({**events[-1], "eventId": "different-id"})
            with self.assertRaisesRegex(AssertionError, "Duplicate"):
                analyze(out, events)


if __name__ == "__main__":
    unittest.main()
