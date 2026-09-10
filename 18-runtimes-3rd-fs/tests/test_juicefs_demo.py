"""Deployment and session tests; file transfer tests use s5cmd only."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/juicefs"))
sys.path.insert(0, str(ROOT / "deploy/juicefs"))
from stack import build_template


class FakeS5:
    def __init__(self):
        self.objects = {}

    def copy(self, source, destination, **kwargs):
        self.objects[str(destination)] = Path(source).read_bytes()
        return {"success": True}


def test_template_has_no_tenant_storage_bypass():
    template = build_template()
    resources = template["Resources"]
    runtime = json.dumps(resources["RuntimeRole"]["Properties"]["Policies"])
    assert "RuntimeRoleA" not in resources and "RuntimeRoleB" not in resources
    for forbidden in ("s3:", "secretsmanager:", "sts:", "bedrock-agentcore:Invoke", "TenantASecret", "TenantBSecret"):
        assert forbidden not in runtime
    for suffix, tenant, other in [("A", "tenant-a", "tenant-b"), ("B", "tenant-b", "tenant-a")]:
        role = resources["TenantDataRole" + suffix]["Properties"]
        text = json.dumps(role["Policies"])
        assert "shared-demo" not in text and other not in text
        assert f"direct/{tenant}/*" in text and "Secret" not in text
        assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"AWS": {"Ref": "ControllerArn"}}
    ingress = resources["GatewaySG"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [{"IpProtocol": "tcp", "FromPort": 9000, "ToPort": 9000,
                        "SourceSecurityGroupId": {"Ref": "RuntimeSG"}}]
    assert resources["DataBucket"]["DeletionPolicy"] == "Retain"
    assert resources["TenantASecret"]["DeletionPolicy"] == "Retain"


def controller():
    spec = importlib.util.spec_from_file_location("juicefs_controller", ROOT / "scripts/05-juicefs-demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_does_not_claim_speedup_for_incomplete_measurements(tmp_path):
    module = controller()
    row = {"workload": "unzip", "workers": 32, "label": "write", "success": True, "wall_seconds": 2,
           "file_batch_seconds": 1, "engine": "s5cmd", "repetition": 0}
    record = {"run_id": "test-run-001", "region": "us-west-2", "repetitions": 3,
              "measurements": [{**row, "backend": "s3"}, {**row, "backend": "juicefs"}]}
    path = tmp_path / "report.md"
    module.write_report(path, record)
    assert "N/A" in path.read_text() and "2.00x" not in path.read_text()


def test_no_cloud_mutation_without_approval(tmp_path):
    module = controller()
    with pytest.raises(ValueError):
        module.deploy(SimpleNamespace(approve_costs=False))
    with pytest.raises(ValueError):
        module.cleanup(SimpleNamespace(destroy_demo=False))


def test_bootstrap_security_invariants():
    script = (ROOT / "deploy/juicefs/bootstrap.sh").read_text()
    assert "--writeback" not in script
    assert "--object-meta" in script and "--keep-etag" in script
    assert "--cacert" in script and "verify=False" not in script
    assert "1015ade83a7a93180a29f6c93ee5780a3eda52522331934ef2ef0cc0921995fd" in script
    assert "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" in script


def test_agent_fixed_tenant_and_nonblocking_guard(monkeypatch):
    spec = importlib.util.spec_from_file_location("juicefs_agent", ROOT / "demo/juicefs/agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    from test_juicefs_sessions import demo_bootstrap
    bootstrap, context = demo_bootstrap()
    assert agent.invoke(bootstrap, context)["success"]
    client = FakeS5()
    agent._clients["s3"] = client
    auth = {"session_token": bootstrap["session_token"]}
    assert agent.invoke({"action": "ping", **auth}, context)["tenant"] == "tenant-a"
    assert not agent.invoke({"action": "ping", "tenant": "tenant-b", **auth}, context)["success"]
    assert agent.invoke({"action": "seed", "backend": "s3", "run_id": "test-run-001", **auth}, context)["success"]
    assert "s3://physical-bucket/direct/tenant-a/test-run-001/isolation/marker.txt" in client.objects
    assert all("tenant-b" not in key for key in client.objects)
    agent._lock.acquire()
    try:
        result = agent.invoke({"action": "seed", "backend": "s3", "run_id": "test-run-001", **auth}, context)
        assert result == {"success": False, "error": "benchmark_already_running"}
    finally:
        agent._lock.release()


def test_al2023_uses_supported_python():
    script = (ROOT / "deploy/juicefs/bootstrap.sh").read_text()
    assert 'python3.12 -m venv' in script  # AL2023 system Python is 3.9.
    assert 'botocore==1.43.87' in script


def test_error_report_excludes_exception_message():
    module = controller()
    exc = ClientError({"Error": {"Code": "AccessDenied", "Message": "secret-token=https://signed.example"},
                       "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "test-request"}}, "GetObject")
    result = module.safe_exception(exc)
    assert result["status"] == 403 and result["code"] == "AccessDenied"
    assert "secret-token" not in json.dumps(result) and "signed.example" not in json.dumps(result)


def test_partial_stack_cleanup_preserves_inventory(tmp_path, monkeypatch):
    module = controller()
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"region": "us-west-2", "account": "123", "runtimes": {}, "stack_id": "stack-test"}))
    resources = [{"LogicalResourceId": "DataBucket", "PhysicalResourceId": "retained-bucket",
                  "ResourceType": "AWS::S3::Bucket", "ResourceStatus": "CREATE_COMPLETE"}]
    deleted = []
    cf = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=lambda **kw: [{"StackResourceSummaries": resources}]),
        delete_stack=lambda **kw: deleted.append(kw),
        get_waiter=lambda name: SimpleNamespace(wait=lambda **kw: None))
    monkeypatch.setattr(module, "api", lambda service, region: {
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "123"}),
        "bedrock-agentcore-control": SimpleNamespace(), "cloudformation": cf}[service])
    assert module.cleanup(SimpleNamespace(destroy_demo=True, state=path)) == 0
    result = json.loads(path.read_text())
    assert result["resource_inventory_before_cleanup"][0]["PhysicalResourceId"] == "retained-bucket"
    assert deleted and result["infrastructure_deleted"]
