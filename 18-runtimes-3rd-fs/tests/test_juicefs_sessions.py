"""Offline tests for one runtime's per-session credential binding."""
import copy
import importlib.util
import io
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo/juicefs"))
from session_binding import SessionBinding, SessionBindingError

def demo_bootstrap(tenant="tenant-a", token="a" * 64):
    context = SimpleNamespace(session_id="session-" + tenant + "-" + "0" * 32)
    credentials = {"AccessKeyId": tenant, "SecretAccessKey": "secret-" + tenant}
    payload = {"action": "initialize", "tenant": tenant, "session_id": context.session_id,
        "session_token": token, "storage": {"region": "us-west-2", "data_bucket": "physical-bucket",
            "juicefs": {"endpoint": "https://example.com:9000", "ca_pem": "-----BEGIN CERTIFICATE-----test",
                        "credentials": credentials.copy()},
            "s3": {"credentials": {**credentials, "SessionToken": "sts-" + tenant}, "expires_at": time.time() + 3600}}}
    return payload, context


def test_two_sessions_independent_binding_and_no_credentials_in_response():
    a, b = SessionBinding(), SessionBinding()
    pa, ca = demo_bootstrap()
    pb, cb = demo_bootstrap("tenant-b", "b" * 64)
    ra, rb = a.bind(pa, ca), b.bind(pb, cb)
    assert ra["session_id"] != rb["session_id"] and ra["process_id"] != rb["process_id"]
    assert a.tenant == "tenant-a" and b.tenant == "tenant-b"
    with pytest.raises(SessionBindingError, match="session_token_mismatch"):
        b.authorize({"session_token": pa["session_token"]}, cb)
    with pytest.raises(SessionBindingError, match="session_id_mismatch"):
        a.authorize(pa, cb)
    encoded = json.dumps([ra, rb])
    for secret in (pa["session_token"], "secret-tenant-a", "sts-tenant-a"):
        assert secret not in encoded


@pytest.mark.parametrize("case", ["no_context", "wrong_context", "wrong_tenant", "no_key", "no_sts_token", "expired", "http"])
def test_initialization_fail_closed(case):
    binding = SessionBinding()
    payload, context = demo_bootstrap()
    if case == "no_context": context = None
    if case == "wrong_context": context.session_id += "x"
    if case == "wrong_tenant": payload["tenant"] = "arbitrary"
    if case == "no_key": del payload["storage"]["juicefs"]["credentials"]["SecretAccessKey"]
    if case == "no_sts_token": del payload["storage"]["s3"]["credentials"]["SessionToken"]
    if case == "expired": payload["storage"]["s3"]["expires_at"] = 1
    if case == "http": payload["storage"]["juicefs"]["endpoint"] = "http://example.com"
    with pytest.raises(SessionBindingError): binding.bind(payload, context)
    with pytest.raises(SessionBindingError, match="session_not_initialized"): binding.describe()


def test_rebinding_and_invalid_refresh_leave_state_intact():
    binding = SessionBinding()
    payload, context = demo_bootstrap()
    binding.bind(payload, context)
    changed = copy.deepcopy(payload)
    changed["tenant"] = "tenant-b"
    with pytest.raises(SessionBindingError, match="immutable"): binding.bind(changed, context)
    changed = copy.deepcopy(payload)
    changed["storage"]["s3"]["expires_at"] = 1
    with pytest.raises(SessionBindingError, match="expired"): binding.bind(changed, context, refresh=True)
    assert binding.storage_config() == payload["storage"]
    changed = copy.deepcopy(payload)
    changed["storage"]["s3"]["credentials"]["AccessKeyId"] = "rotated"
    binding.bind(changed, context)  # Idempotent init doesn't update credentials.
    assert binding.storage_config()["s3"]["credentials"]["AccessKeyId"] == "tenant-a"
    binding.bind(changed, context, refresh=True)
    assert binding.storage_config()["s3"]["credentials"]["AccessKeyId"] == "rotated"
    assert binding.tenant == "tenant-a"


def test_expiry_and_missing_token_block_use():
    binding = SessionBinding()
    payload, context = demo_bootstrap()
    with pytest.raises(SessionBindingError): binding.authorize(payload, context)
    binding.bind(payload, context)
    with pytest.raises(SessionBindingError, match="token"): binding.authorize({}, context)
    with pytest.raises(SessionBindingError, match="token"): binding.authorize({"session_token": "b" * 64}, context)
    binding._state["storage"]["s3"]["expires_at"] = 1
    with pytest.raises(SessionBindingError, match="expired"): binding.storage_config()


def controller():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("single_runtime_controller", ROOT / "scripts/05-juicefs-demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_uses_one_arn_two_session_ids(monkeypatch):
    module = controller()
    created = []
    class Session:
        def __init__(self, arn, region, **kwargs):
            self.runtime_arn = arn
            self.session_id = "session-" + str(len(created)) + "x" * 32
            created.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(module, "RuntimeSession", Session)
    with ExitStack() as stack:
        sessions = module.open_tenant_sessions(stack, {"runtime": {"arn": "one-runtime"}, "region": "us-west-2"})
    assert set(sessions) == {"tenant-a", "tenant-b"}
    assert {s.runtime_arn for s in sessions.values()} == {"one-runtime"}
    assert len({s.session_id for s in sessions.values()}) == 2


@pytest.mark.parametrize("status,session_id,good", [(200, "session-test", True), (200, "wrong", False), (500, "session-test", False)])
def test_controller_verifies_response_routing(status, session_id, good):
    module = controller()
    stream = io.BytesIO(b'{"success": true}')
    session = SimpleNamespace(runtime_arn="arn", session_id="session-test", client=SimpleNamespace(
        invoke_agent_runtime=lambda **kw: {"statusCode": status, "runtimeSessionId": session_id, "response": stream}))
    if good:
        assert module.invoke(session, {})["success"]
    else:
        with pytest.raises(RuntimeError): module.invoke(session, {})
    assert stream.closed


def test_agent_clients_use_explicit_credentials(monkeypatch):
    spec = importlib.util.spec_from_file_location("session_agent_client", ROOT / "demo/juicefs/agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    payload, context = demo_bootstrap()
    assert agent.invoke(payload, context)["success"]
    seen = []
    monkeypatch.setattr(agent, "S5Client", lambda endpoint, region, credentials: seen.append(credentials) or SimpleNamespace(version=lambda: "v2.3.0"))
    agent.storage("s3")
    assert seen == [payload["storage"]["s3"]["credentials"]]
    assert "SessionToken" in seen[0]


def test_controller_only_reads_selected_tenant_secret_and_role(monkeypatch):
    from datetime import datetime, timezone
    module = controller()
    calls = []
    gateway = demo_bootstrap()[0]["storage"]["juicefs"]
    sm = SimpleNamespace(get_secret_value=lambda **kw: calls.append(("secret", kw)) or {"SecretString": json.dumps(gateway)})
    sts = SimpleNamespace(assume_role=lambda **kw: calls.append(("sts", kw)) or {"Credentials": {
        "AccessKeyId": "scoped", "SecretAccessKey": "secret", "SessionToken": "token",
        "Expiration": datetime.now(timezone.utc)}})
    monkeypatch.setattr(module, "api", lambda name, region: {"secretsmanager": sm, "sts": sts}[name])
    state = {"region": "us-west-2", "outputs": {"DataBucket": "data-bucket", "TenantBSecretArn": "secret-b",
                                               "TenantDataRoleB": "role-b"}}
    result = module.session_bootstrap(state, "tenant-b", "session-" + "b" * 32, "b" * 64)
    assert calls[0] == ("secret", {"SecretId": "secret-b"})
    assert calls[1][1]["RoleArn"] == "role-b" and calls[1][1]["DurationSeconds"] == 3600
    assert result["storage"]["s3"]["credentials"]["AccessKeyId"] == "scoped"
    assert result["tenant"] == "tenant-b" and result["action"] == "initialize"
    with pytest.raises(KeyError): module.session_bootstrap(state, "other", "session", "token")


def test_parallel_initialize_cannot_rebind_tenant():
    from concurrent.futures import ThreadPoolExecutor
    spec = importlib.util.spec_from_file_location("session_agent_concurrent", ROOT / "demo/juicefs/agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    a, context = demo_bootstrap()
    b, _ = demo_bootstrap("tenant-b", "b" * 64)
    b["session_id"] = context.session_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: agent.invoke(p, context), [a, b]))
    assert sum(result["success"] for result in results) == 1
    winner = next(result["tenant"] for result in results if result["success"])
    token = a["session_token"] if winner == "tenant-a" else b["session_token"]
    assert agent.invoke({"action": "ping", "session_token": token}, context)["tenant"] == winner
