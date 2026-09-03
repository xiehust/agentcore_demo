import json
import socket
from datetime import datetime, timezone

import pytest

import network_audit as na
import tool_audit_hooks as ta

NOW = datetime(2026, 9, 3, 7, 30, 12, 412000, tzinfo=timezone.utc)


def test_redact_nested_and_truncate():
    value = {"url": "https://api", "headers": {"Authorization": "Bearer abc", "X-Api-Key": "k"},
             "items": [{"password": "p", "ok": "x" * 20}]}
    out = ta.redact(value, max_chars=10)
    assert out["headers"] == {"Authorization": ta.REDACTED, "X-Api-Key": ta.REDACTED}
    assert out["items"][0]["password"] == ta.REDACTED
    assert out["items"][0]["ok"].startswith("xxxxxxxxxx...[+10 chars]")


def test_build_audit_record_before_and_after():
    policy = ta.AuditPolicy(deny_tools=frozenset({"shell"}))
    before = ta.build_audit_record(phase="before", tool_name="shell", tool_use_id="t1",
                                   tool_input={"command": "rm -rf /", "token": "s"}, policy=policy,
                                   agent_name="demo", session_id="sess", otel={"trace_id": "a" * 32}, now=NOW)
    assert before["decision"] == "deny" and before["input"]["token"] == ta.REDACTED
    assert before["ts"] == "2026-09-03T07:30:12.412Z" and before["trace_id"] == "a" * 32
    assert "status" not in before

    after = ta.build_audit_record(phase="after", tool_name="http_request", tool_use_id="t2", tool_input={},
                                  policy=policy, result={"login": "octo"}, status="success", duration_ms=231.26, now=NOW)
    assert after["decision"] == "allow" and after["status"] == "success" and after["duration_ms"] == 231.3
    assert json.loads(after["output_preview"]) == {"login": "octo"}
    json.dumps(after)  # serialisable


def test_build_audit_record_rejects_bad_phase():
    with pytest.raises(ValueError):
        ta.build_audit_record(phase="during", tool_name="x", tool_use_id="1", tool_input={}, policy=ta.AuditPolicy())


def test_tool_audit_hook_denies_and_records_with_fake_events():
    records = []
    hook = ta.ToolAuditHook(ta.AuditPolicy(deny_tools=frozenset({"shell"})), sink=records.append, session_id="s")

    class Event:
        def __init__(self, name, use_id, result=None):
            self.tool_use = {"name": name, "toolUseId": use_id, "input": {"x": 1}}
            self.agent = type("A", (), {"name": "agent"})()
            self.cancel_tool = None
            self.result = result

    denied = Event("shell", "u1")
    hook.before_tool(denied)
    assert denied.cancel_tool and records[-1]["decision"] == "deny"

    allowed = Event("file_read", "u2", result={"status": "success", "content": [{"text": "hi"}]})
    hook.before_tool(allowed)
    assert allowed.cancel_tool is None
    hook.after_tool(allowed)
    assert records[-1]["phase"] == "after" and records[-1]["status"] == "success" and "duration_ms" in records[-1]


def test_host_allowed_globs():
    assert na.host_allowed("api.github.com", {"api.github.com"})
    assert na.host_allowed("s3.us-east-2.amazonaws.com", {"*.amazonaws.com"})
    assert not na.host_allowed("evil.example", {"*.amazonaws.com"})
    assert na.host_allowed("anything", None)


def test_build_connect_record_uses_reverse_cache():
    na.remember_resolution("api.github.com", ["140.82.112.5"])
    rec = na.build_connect_record(("140.82.112.5", 443), allow_hosts={"api.github.com"}, outcome="attempt", now=NOW)
    assert rec["host"] == "api.github.com" and rec["port"] == 443 and rec["decision"] == "allow"
    rec = na.build_connect_record(("10.0.0.9", 22), allow_hosts={"api.github.com"}, outcome="attempt", now=NOW)
    assert rec["decision"] == "deny" and rec["ts"] == "2026-09-03T07:30:12.412Z"
    unix = na.build_connect_record("/run/x.sock", allow_hosts=None, outcome="attempt", now=NOW)
    assert unix["port"] == -1


def test_install_network_audit_blocks_disallowed_connect(monkeypatch):
    records = []
    monkeypatch.setattr(na, "_installed", False)
    original = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex)
    try:
        assert na.install_network_audit(allow_hosts={"127.0.0.1"}, sink=records.append)
        assert not na.install_network_audit()  # idempotent
        s = socket.socket()
        with pytest.raises(PermissionError):
            s.connect(("10.255.255.1", 9))
        assert records[-1]["decision"] == "deny" and records[-1]["outcome"] == "blocked"
        assert s.connect_ex(("10.255.255.1", 9)) == 13
        s.close()
    finally:
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = original
        monkeypatch.setattr(na, "_installed", False)
