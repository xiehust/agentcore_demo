import base64
import json

import pytest

import runtime_cmd as rc


def _stream(*events):
    return list(events)


def test_parse_valid_stream_folds_stdout_and_stderr():
    result = rc.parse_command_stream(_stream(
        {"chunk": {"contentStart": {}}},
        {"chunk": {"contentDelta": {"stdout": "a"}}},
        {"chunk": {"contentDelta": {"stdout": "b", "stderr": "w"}}},
        {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}},
    ))
    assert result["stdout"] == "ab" and result["stderr"] == "w"
    assert result["content_start_count"] == 1 and not result["protocol_errors"]


@pytest.mark.parametrize("events,expected", [
    ([{"chunk": {"contentDelta": {"stdout": "x"}}}, {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}], "ordering"),
    ([{"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}, {"chunk": {"contentStart": {}}}], "ordering"),
    ([{"chunk": {"contentStart": {}}}], "stream ended without contentStop"),
    ([{"chunk": {"contentStart": {}}}, {"chunk": {"contentStop": {"exitCode": 0, "status": "TIMED_OUT"}}}], "status 'TIMED_OUT'"),
    ([{"chunk": {"contentStart": {}}}, {"chunk": {"contentStop": {"exitCode": 2, "status": "COMPLETED"}}}], "exitCode 2"),
    ([{"chunk": {"contentStart": {}}}, {"accessDeniedException": {"message": "no"}}, {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}], "exception"),
])
def test_command_error_rejects_bad_streams(events, expected):
    result = {"api_status": 200, "runtime_session_id": "s", "expected_session_id": "s", **rc.parse_command_stream(events)}
    assert expected in rc.command_error(result)


def test_command_error_rejects_session_mismatch_and_non_200():
    good = rc.parse_command_stream([{"chunk": {"contentStart": {}}}, {"chunk": {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}}])
    assert "session" in rc.command_error({"api_status": 200, "runtime_session_id": "a", "expected_session_id": "b", **good})
    assert "API status" in rc.command_error({"api_status": 500, "runtime_session_id": "a", "expected_session_id": "a", **good})
    assert rc.command_error({"api_status": 200, "runtime_session_id": "a", "expected_session_id": "a", **good}) is None


def test_encode_shell_script_uses_explicit_bash_and_roundtrips():
    script = "set -e\necho 'hi' | tr a-z A-Z\n"
    command = rc.encode_shell_script(script)
    assert command.startswith("/bin/bash -c ")
    encoded = command.split("printf '%s' '", 1)[1].split("'", 1)[0]
    assert base64.b64decode(encoded).decode() == script
    assert json.loads(command[len("/bin/bash -c "):]).endswith("| base64 -d | /bin/bash")


def test_session_id_validation():
    assert len(rc.new_session_id("probe")) >= 33
    with pytest.raises(ValueError):
        rc.validate_session_id("short")
    with pytest.raises(ValueError):
        rc.validate_session_id("a" * 40 + "_bad")


def test_retry_conflicts_only_retries_409():
    class Conflict(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 409}}

    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Conflict()
        return "ok"

    assert rc.retry_conflicts(op, sleep=lambda _: None) == "ok" and calls["n"] == 3

    def bad():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rc.retry_conflicts(bad, sleep=lambda _: None)
