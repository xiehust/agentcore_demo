"""Focused no-AWS tests for short/long load-test result contracts."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from load_test import invoke_short, run_level as run_short_level  # noqa: E402
from load_test_longrun import (  # noqa: E402
    attach_verification,
    invoke_phase,
    invoke_project,
    run_level as run_long_level,
    verify_artifacts,
)


INSTANCE = {
    "boot_id": "boot-1",
    "server_run_id": "run-1",
    "pid": 10,
    "hostname": "host-1",
}


class InvokeSession:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses

    def invoke(self, _user_id: str, _prompt: str, *, reset: bool) -> dict[str, Any]:
        del reset
        return dict(self.responses.pop(0))


class DuplicateWorkspaceSession:
    def invoke(self, user_id: str, prompt: str, *, reset: bool) -> dict[str, Any]:
        if "Reply with exactly:" in prompt:
            result = prompt.rsplit(":", 1)[1].strip()
        elif reset:
            result = "PHASE-1-DONE 4"
        else:
            result = "PROJECT-DONE 6"
        return {
            "success": True,
            "result": result,
            "claude_session_id": f"{user_id}-c{'1' if reset else '2'}",
            "resumed_from": None if reset else f"{user_id}-c1",
            "workspace": "/tmp/agentcore-users/incorrectly-shared",
            "instance": INSTANCE,
            "latency_ms": 1.0,
            "tool_call_count": 1,
        }


class ShellSession:
    def __init__(self) -> None:
        self.script = ""

    def run_shell_script(self, script: str, *, timeout: int) -> dict[str, Any]:
        del timeout
        self.script = script
        return {
            "stdout": '{"user-1":{"success":false,"errors":["fake"]}}\n',
            "api_status": 200,
            "content_stop": {"status": "COMPLETED", "exitCode": 0},
            "stream_exceptions": [],
        }


class TestExactMarkersAndFingerprints(unittest.TestCase):
    def test_short_marker_must_be_the_entire_reply(self):
        session = InvokeSession(
            [
                {
                    "success": True,
                    "result": "prefix PONG-1 suffix",
                    "claude_session_id": "c1",
                    "resumed_from": None,
                    "workspace": "/tmp/agentcore-users/u1",
                    "instance": INSTANCE,
                    "latency_ms": 1.0,
                }
            ]
        )
        record = invoke_short(cast(Any, session), "user-1", "PONG-1", None, INSTANCE)
        self.assertFalse(record["marker_ok"])
        self.assertFalse(record["contract_success"])

    def test_long_phase_requires_marker_but_allows_a_summary(self):
        session = InvokeSession(
            [
                {
                    "success": True,
                    "result": "Validation summary\\nPROJECT-DONE 6 (extra)",
                    "latency_ms": 1.0,
                },
                {
                    "success": True,
                    "result": "work finished without the expected token",
                    "latency_ms": 1.0,
                },
            ]
        )
        accepted = invoke_phase(
            cast(Any, session),
            "user-1",
            {"name": "qa", "marker": "PROJECT-DONE 6", "prompt": "work"},
            reset=True,
        )
        rejected = invoke_phase(
            cast(Any, session),
            "user-1",
            {"name": "qa", "marker": "PROJECT-DONE 6", "prompt": "work"},
            reset=True,
        )
        self.assertTrue(accepted["marker_ok"])
        self.assertTrue(accepted["phase_success"])
        self.assertFalse(rejected["marker_ok"])
        self.assertFalse(rejected["phase_success"])

    def test_both_ramps_fail_duplicate_user_workspaces(self):
        session = cast(Any, DuplicateWorkspaceSession())
        short = run_short_level(session, 2, "run1", INSTANCE)
        self.assertEqual(short["distinct_workspaces"], 1)
        self.assertEqual(short["success"], 0)
        self.assertTrue(all(not item["unique_workspace"] for item in short["requests"]))

        long = run_long_level(session, 2, "run1", INSTANCE)
        self.assertEqual(long["distinct_workspaces"], 1)
        self.assertEqual(long["agent_success"], 0)
        self.assertTrue(all(not item["unique_workspace"] for item in long["requests"]))

    def test_long_project_compares_the_whole_process_fingerprint(self):
        changed_process = {**INSTANCE, "pid": 11}
        responses = [
            {
                "success": True,
                "result": "PHASE-1-DONE 4",
                "claude_session_id": "c1",
                "resumed_from": None,
                "workspace": "/tmp/agentcore-users/u1",
                "instance": INSTANCE,
                "latency_ms": 1.0,
            },
            {
                "success": True,
                "result": "PROJECT-DONE 6",
                "claude_session_id": "c2",
                "resumed_from": "c1",
                "workspace": "/tmp/agentcore-users/u1",
                "instance": changed_process,
                "latency_ms": 1.0,
            },
        ]
        result = invoke_project(
            cast(Any, InvokeSession(responses)),
            "user-1",
            "token-1",
            threading.Barrier(1),
            INSTANCE,
        )
        self.assertFalse(result["process_consistent"])
        self.assertFalse(result["agent_success"])


class TestVerificationContracts(unittest.TestCase):
    def test_verified_success_never_overwrites_agent_success(self):
        summary = {
            "level": 2,
            "requests": [
                {"user_id": "u1", "agent_success": True},
                {"user_id": "u2", "agent_success": False},
            ],
        }
        attach_verification(
            summary,
            {
                "u1": {"success": True, "errors": []},
                "u2": {"success": True, "errors": []},
            },
        )
        self.assertEqual(summary["artifact_success"], 2)
        self.assertEqual(summary["verified_success"], 1)
        self.assertTrue(summary["requests"][0]["verified_success"])
        self.assertFalse(summary["requests"][1]["verified_success"])

    def test_verifier_specs_are_base64_not_shell_interpolation(self):
        hostile_workspace = "/tmp/agentcore-users/x'; touch /tmp/pwned; #"
        session = ShellSession()
        value, _command = verify_artifacts(
            cast(Any, session),
            [
                {
                    "user_id": "user-1",
                    "workspace": hostile_workspace,
                    "run_token": "$(touch /tmp/also-pwned)",
                }
            ],
        )
        self.assertIn("user-1", value)
        self.assertNotIn(hostile_workspace, session.script)
        self.assertNotIn("also-pwned", session.script)
        self.assertIn("base64 -d", session.script)
        self.assertNotIn(json.dumps(hostile_workspace), session.script)


if __name__ == "__main__":
    unittest.main()
