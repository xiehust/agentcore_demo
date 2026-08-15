"""Unit tests for scripts/runtime_session.py; no AWS calls are made."""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from runtime_session import (  # noqa: E402
    CommandExecutionError,
    RuntimeConfigError,
    RuntimeSession,
    SSEParseError,
    SessionStopError,
    atomic_write_json,
    cleanup_session,
    finalize_before_session_stop,
    load_runtime_config,
    new_session_id,
    parse_command_stream,
    parse_levels,
    parse_monitor_csv,
    parse_sse,
    retry_conflicts,
    validate_session_id,
    window_stats,
)

RUNTIME = {
    "region": "us-west-2",
    "runtimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo",
}
SESSION_ID = "shared-session-1234567890123456789"


class Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self):
        return self.value


class FakeClient:
    def __init__(
        self,
        command_response: Any = None,
        invoke_response: Any = None,
        stop_response: Any = None,
    ) -> None:
        self.command_response = command_response
        self.invoke_response = invoke_response
        self.stop_response = stop_response
        self.command_request: dict[str, Any] | None = None
        self.invoke_request: dict[str, Any] | None = None
        self.stop_request: dict[str, Any] | None = None

    def invoke_agent_runtime_command(self, **request):
        self.command_request = request
        return self.command_response

    def invoke_agent_runtime(self, **request):
        self.invoke_request = request
        return self.invoke_response

    def stop_runtime_session(self, **request):
        self.stop_request = request
        return self.stop_response


def command_response(*events, status=200, session_id=SESSION_ID):
    return {
        "statusCode": status,
        "runtimeSessionId": session_id,
        "stream": iter(events),
    }


class TestConfiguration(unittest.TestCase):
    def test_session_ids_and_levels(self):
        generated = new_session_id("shared test")
        self.assertEqual(validate_session_id(generated), generated)
        with self.assertRaises(ValueError):
            validate_session_id("too-short")
        self.assertEqual(parse_levels("1,2,8"), [1, 2, 8])
        self.assertEqual(parse_levels("[2, 4]"), [2, 4])
        with self.assertRaises(ValueError):
            parse_levels("1,0")
        for invalid in ("[2.5]", "[true]", "[2,2]", [False], [1.5]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_levels(invalid)

    def test_runtime_config_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            path.write_text(json.dumps(RUNTIME))
            self.assertEqual(load_runtime_config(path), RUNTIME)
            path.write_text("[]")
            with self.assertRaises(RuntimeConfigError):
                load_runtime_config(path)


class TestSSE(unittest.TestCase):
    def test_parses_json_events_and_done(self):
        raw = (
            b'data: {"event":"delta","text":"hello"}\n\n'
            b'data: {"event":"complete","result":"ok"}\n\n'
            b"data: [DONE]\n\n"
        )
        events = parse_sse(raw)
        self.assertEqual([item["event"] for item in events], ["delta", "complete"])

    def test_rejects_malformed_or_non_object_data(self):
        with self.assertRaises(SSEParseError):
            parse_sse("data: {nope}\n\n")
        with self.assertRaises(SSEParseError):
            parse_sse("data: [1,2]\n\n")

    def test_runtime_invoke_extracts_contract(self):
        response = {
            "statusCode": 200,
            "response": Body(
                b'data: {"event":"tool","name":"Read"}\n\n'
                b'data: {"event":"complete","result":"PONG",'
                b'"workspace":"/tmp/agentcore-users/a",'
                b'"claude_session_id":"c1","resumed_from":null,'
                b'"instance":{"server_run_id":"r1"}}\n\n'
            ),
        }
        client = FakeClient(invoke_response=response)
        session = RuntimeSession(RUNTIME, SESSION_ID, client)
        result = session.invoke("alice", "say PONG", reset=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["tool_call_count"], 1)
        self.assertEqual(result["workspace"], "/tmp/agentcore-users/a")
        request = client.invoke_request
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["runtimeUserId"], "alice")


class TestCommandEvents(unittest.TestCase):
    def test_folds_stdout_stderr_and_stop(self):
        result = parse_command_stream(
            iter(
                (
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentDelta": {"stdout": "hello", "stderr": "warn"}}},
                    {"chunk": {"contentDelta": {"stdout": " world", "stderr": "!"}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                )
            )
        )
        self.assertEqual(result["content_start_count"], 1)
        self.assertEqual(result["stdout"], "hello world")
        self.assertEqual(result["stderr"], "warn!")
        self.assertEqual(result["content_stop"]["exitCode"], 0)
        self.assertEqual(result["protocol_errors"], [])
        self.assertEqual(result["unknown_events"], [])

    def test_protocol_errors_and_unknown_members_are_recorded(self):
        cases = (
            (
                (
                    {"chunk": {"contentDelta": {"stdout": "early"}}},
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {"unexpected": True}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentDelta": {"stdout": b"not-a-model-string"}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {}}},
                    {
                        "chunk": {
                            "contentStop": {"exitCode": False, "status": "COMPLETED"}
                        }
                    },
                ),
                "protocol_errors",
            ),
            (
                (
                    {"chunk": {"contentStart": {}}},
                    {"chunk": {"contentStop": {"exitCode": 0, "status": "FUTURE"}}},
                ),
                "protocol_errors",
            ),
            (({"chunk": {"futureContent": {}}},), "unknown_events"),
            (({"futureException": {"message": "nope"}},), "unknown_events"),
            (({"chunk": {"contentStart": {}, "contentDelta": {}}},), "unknown_events"),
        )
        for events, field in cases:
            with self.subTest(events=events):
                result = parse_command_stream(iter(events))
                self.assertTrue(result[field])

    def test_stream_iteration_failure_is_an_exception_event(self):
        def broken_stream():
            yield {"chunk": {"contentStart": {}}}
            raise RuntimeError("stream reset")

        result = parse_command_stream(broken_stream())
        self.assertEqual(result["stream_exceptions"][0]["type"], "RuntimeError")

    def test_command_requires_every_success_signal(self):
        success_events = (
            {"chunk": {"contentStart": {}}},
            {"chunk": {"contentDelta": {"stdout": "ok"}}},
            {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
        )
        client = FakeClient(command_response=command_response(*success_events))
        session = RuntimeSession(RUNTIME, SESSION_ID, client)
        result = session.command("printf ok", require_success=True)
        self.assertTrue(result["success"])
        request = client.command_request
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["body"]["command"], "printf ok")

        failures = (
            command_response(*success_events, status=500),
            command_response(
                {"runtimeClientError": {"message": "runtime failed"}},
                success_events[-1],
            ),
            command_response({"chunk": {"contentStart": {}}}),
            command_response(
                {"chunk": {"contentStart": {}}},
                {"chunk": {"contentStop": {"exitCode": 1, "status": "COMPLETED"}}},
            ),
            command_response(
                {"chunk": {"contentStart": {}}},
                {"chunk": {"contentStop": {"exitCode": 0, "status": "TIMED_OUT"}}},
            ),
            command_response(
                {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}
            ),
            command_response(*success_events, session_id="x" * 33),
            command_response(
                {"chunk": {"contentStart": {}}},
                {"chunk": {"futureContent": {}}},
                success_events[-1],
            ),
            command_response({}),
        )
        for response in failures:
            with self.subTest(response=response):
                client.command_response = response
                with self.assertRaises(CommandExecutionError):
                    session.command("false", require_success=True)

    def test_shell_script_is_base64_data_not_interpolated_shell(self):
        events = (
            {"chunk": {"contentStart": {}}},
            {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
        )
        client = FakeClient(command_response=command_response(*events))
        session = RuntimeSession(RUNTIME, SESSION_ID, client)
        script = "printf '%s\\n' \"$(touch /tmp/not-executed)\""
        session.run_shell_script(script)
        request = client.command_request
        assert request is not None
        command = request["body"]["command"]
        self.assertNotIn(script, command)
        match = re.fullmatch(
            r"/bin/bash -c \"printf '%s' '([A-Za-z0-9+/=]+)' \| base64 -d \| /bin/bash\"",
            command,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(base64.b64decode(match.group(1)).decode(), script)


class TestRetryAndStop(unittest.TestCase):
    def test_retries_only_conflict(self):
        calls = []
        sleeps = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                return {"statusCode": 409}
            return {"statusCode": 200}

        result = retry_conflicts(operation, attempts=4, sleep=sleeps.append)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

        with self.assertRaises(ValueError):
            retry_conflicts(lambda: (_ for _ in ()).throw(ValueError("bad")))

    def test_validates_stop_response(self):
        client = FakeClient(
            stop_response={"statusCode": 200, "runtimeSessionId": SESSION_ID}
        )
        session = RuntimeSession(RUNTIME, SESSION_ID, client)
        self.assertTrue(session.stop()["success"])
        request = client.stop_request
        self.assertIsNotNone(request)
        assert request is not None
        self.assertIn("clientToken", request)

        client.stop_response = {"statusCode": 200, "runtimeSessionId": "x" * 33}
        with self.assertRaises(SessionStopError):
            session.stop()

    def test_cleanup_records_stop_failure_and_debug_retention(self):
        client = FakeClient(
            stop_response={"statusCode": 500, "runtimeSessionId": SESSION_ID}
        )
        session = RuntimeSession(RUNTIME, SESSION_ID, client)
        failed = cleanup_session(session, keep_session=False)
        self.assertTrue(failed["attempted"])
        self.assertFalse(failed["success"])
        self.assertIn("SessionStopError", failed["error"])

        client.stop_request = None
        kept = cleanup_session(session, keep_session=True)
        self.assertTrue(kept["kept_for_debug"])
        self.assertIn("billable", kept["warning"])
        self.assertIsNone(client.stop_request)

    def test_finalization_cannot_skip_session_stop(self):
        calls = []

        def interrupted_monitor():
            calls.append("monitor")
            raise KeyboardInterrupt("operator interrupt")

        def stop_session():
            calls.append("stop")

        with self.assertRaises(KeyboardInterrupt):
            finalize_before_session_stop(interrupted_monitor, stop_session)
        self.assertEqual(calls, ["monitor", "stop"])


class TestMonitoringAndCheckpoints(unittest.TestCase):
    HEADER = (
        "epoch,cpu_pct,mem_used_mb,mem_avail_mb,load1,agent_procs,"
        "cgroup_memory_current_mb,cgroup_memory_max_mb\n"
    )

    def test_csv_parse_and_window_stats(self):
        samples = parse_monitor_csv(
            self.HEADER
            + "100.0,10,500,1500,0.5,2,400,2048\n"
            + "102.0,30,700,1300,1.5,4,600,2048\n"
        )
        stats = window_stats(samples, 100.0, 102.0)
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["cpu_avg_pct"], 20.0)
        self.assertEqual(stats["mem_avail_min_mb"], 1300.0)
        self.assertEqual(stats["agent_procs_max"], 4)
        self.assertEqual(stats["cgroup_memory_current_max_mb"], 600.0)
        self.assertEqual(window_stats(samples, 200.0, 202.0), {})

    def test_csv_rejects_wrong_header_or_value(self):
        with self.assertRaises(ValueError):
            parse_monitor_csv("epoch,cpu\n1,2\n")
        with self.assertRaises(ValueError):
            parse_monitor_csv(self.HEADER + "oops,1,2,3,4,5,6,7\n")

    def test_atomic_json_replaces_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            atomic_write_json(path, {"stage": 1})
            atomic_write_json(path, {"stage": 2})
            self.assertEqual(json.loads(path.read_text()), {"stage": 2})
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
