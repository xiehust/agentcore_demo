"""s5cmd adapter contracts, without network or storage SDK calls."""
import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo/juicefs"))
from s5_transfer import S5Client, TransferError, command_line, isolation_checks

def client():
    return S5Client("https://example.com", "us-west-2", {"AccessKeyId": "tenant", "SecretAccessKey": "secret"})


def test_child_credentials_do_not_change_parent_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "broad-parent-role")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://wrong.example")
    c = client()
    assert c.env["AWS_ACCESS_KEY_ID"] == "tenant" and "S3_ENDPOINT_URL" not in c.env
    import os
    assert os.environ["AWS_ACCESS_KEY_ID"] == "broad-parent-role"
    with pytest.raises(ValueError): S5Client(None, "us-west-2", {"AccessKeyId": "x", "SecretAccessKey": "y"})
    with pytest.raises(ValueError): S5Client("http://example.com", "us-west-2", {})


def test_quoted_batch_with_literal_wildcards_and_hidden_files(tmp_path, monkeypatch):
    c = client()
    pairs = [(tmp_path / ".git/a b[1]*'quote", "s3://bucket/bench/a b[1]*'quote", 12)]
    captured = []
    def execute(args, **kwargs):
        captured.append((args, kwargs))
        return {"success": True, "completed_copies": 1}
    monkeypatch.setattr(c, "execute", execute)
    command_file = tmp_path / "commands"
    c.batch(pairs, command_file, workers=32, timeout=10)
    parsed = shlex.split(command_file.read_text())
    assert parsed[:6] == ["cp", "--raw", "--concurrency", "1", "--no-follow-symlinks", str(pairs[0][0])]
    assert parsed[-1] == pairs[0][1]
    assert captured[0][0] == ["run", str(command_file)]
    assert captured[0][1]["expected"] == pairs
    with pytest.raises(ValueError): command_line(["cp", "bad\npath", "target"])


@pytest.mark.parametrize("case", ["ok", "duplicate", "wrong-size", "missing", "json-error", "plain-error", "timeout"])
def test_process_output_reconciled_not_just_exit_code(case, monkeypatch):
    c = client()
    expected = [("/local", "s3://bucket/key", 42)]
    def run(argv, **kwargs):
        assert argv[:5] == ["s5cmd", "--json", "--retry-count", "2", "--numworkers"]
        assert kwargs["env"]["AWS_SECRET_ACCESS_KEY"] == "secret"
        if case == "timeout": raise subprocess.TimeoutExpired(argv, 1)
        record = {"operation": "cp", "success": True, "source": "/local", "destination": "s3://bucket/key", "object": {"size": 42}}
        if case == "wrong-size": record["object"]["size"] = 3
        if case != "missing": kwargs["stdout"].write((json.dumps(record) + "\n").encode())
        if case == "duplicate": kwargs["stdout"].write((json.dumps(record) + "\n").encode())
        if case == "json-error": kwargs["stderr"].write(b'{"error":"AccessDenied: Access Denied"}\n')
        if case == "plain-error": kwargs["stderr"].write(b'bad configuration\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    result = c.execute(["run", "commands"], workers=32, timeout=1, expected=expected)
    assert result["success"] == (case == "ok")
    assert "secret" not in json.dumps(result)


def test_copy_failure_and_zero_exit_skip_rejected(monkeypatch):
    c = client()
    monkeypatch.setattr(c, "execute", lambda *a, **kw: {"success": True, "completed_copies": 0})
    with pytest.raises(TransferError): c.copy("source", "target", expected_size=0)


@pytest.mark.parametrize("denied,timed_out,code,passed", [(True,False,1,True),(False,False,1,False),(True,True,-1,False),(True,False,0,False)])
def test_isolation_requires_access_denied(denied,timed_out,code,passed):
    c = SimpleNamespace(execute=lambda *a, **kw: {"exit_code":code,"timed_out":timed_out,"access_denied":denied})
    result = isolation_checks(c,"tenant-a","tenant-b","bench","bench","test-run-001")
    assert len(result["checks"]) == 6 and result["success"] == passed


def test_zero_byte_json_size_omission(monkeypatch):
    def run(argv, **kwargs):
        kwargs["stdout"].write(b'{"operation":"cp","success":true,"source":"/a","destination":"s3://b/a","object":{}}\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess,"run",run)
    assert client().execute(["run","commands"],expected=[("/a","s3://b/a",0)])["success"]
    assert not client().execute(["run","commands"],expected=[("/a","s3://b/a",1)])["success"]


def test_single_copy_checks_exact_pair(monkeypatch):
    captured=[]
    c=client()
    monkeypatch.setattr(c,"execute",lambda *a,**kw: captured.append(kw) or {"success":True,"completed_copies":1})
    c.copy("s3://b/key","/destination",expected_size=42)
    assert captured[0]["expected"] == [("s3://b/key","/destination",42)]


def test_empty_batch_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        client().batch([], tmp_path / "commands", workers=32, timeout=1)


def test_old_or_empty_cloud_report_rejected():
    import importlib.util
    path=Path(__file__).resolve().parents[1]/"scripts/08-verify-benchmark-results.py"
    spec=importlib.util.spec_from_file_location("s5_validator",path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with pytest.raises(AssertionError):
        module.validate({"success":True,"finished_at":"done","schema":"old","engine":"boto3"})
    with pytest.raises(AssertionError):
        module.validate({"success":True,"finished_at":"done","schema":"s5cmd-workspace-v1","engine":"s5cmd","repetitions":0,"measurements":[]})


def test_update_requires_explicit_approval():
    import importlib.util
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/"scripts"))
    spec=importlib.util.spec_from_file_location("s5_update_guard",root/"scripts/05-juicefs-demo.py")
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    with pytest.raises(ValueError): module.update_runtime(SimpleNamespace(approve_costs=False))


@pytest.mark.parametrize("operation,message,expected", [
    ("rm", 'User: arn:aws:sts::123:assumed-role/tenant/test is not authorized to perform: s3:DeleteObject on resource: "arn:aws:s3:::b/key" because no identity-based policy allows the s3:DeleteObject action', True),
    ("cp", 'User: arn:aws:sts::123:assumed-role/tenant/test is not authorized to perform: s3:DeleteObject on resource: "arn:aws:s3:::b/key" because no identity-based policy allows the s3:DeleteObject action', False),
    ("rm", "connection timeout", False),
    ("rm", "InvalidAccessKeyId", False),
])
def test_enhanced_s3_delete_denial(monkeypatch, operation, message, expected):
    def run(argv, **kwargs):
        kwargs["stderr"].write((json.dumps({"operation":operation,"error":message})+"\n").encode())
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(subprocess,"run",run)
    result=client().execute([operation,"s3://bucket/key"])
    assert result["access_denied"] is expected and not result["success"]
