import base64
import io
import json

import pytest
from botocore.credentials import ReadOnlyCredentials

import git_askpass
import lambda_function as broker
import vault_aws_iam_login as vault


def test_vault_login_payload_is_signed_getcalleridentity():
    creds = ReadOnlyCredentials("AKIAEXAMPLE", "secret", "session-token")
    payload = vault.build_login_payload("agentcore-runtime", credentials=creds, region="us-east-2", server_id="vault.internal")
    assert payload["role"] == "agentcore-runtime"
    assert payload["iam_http_request_method"] == "POST"
    assert base64.b64decode(payload["iam_request_url"]).decode() == "https://sts.us-east-2.amazonaws.com/"
    assert base64.b64decode(payload["iam_request_body"]).decode() == "Action=GetCallerIdentity&Version=2011-06-15"
    headers = json.loads(base64.b64decode(payload["iam_request_headers"]))
    assert headers["X-Vault-AWS-IAM-Server-ID"] == ["vault.internal"]
    assert headers["X-Amz-Security-Token"] == ["session-token"]
    auth = headers["Authorization"][0]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "x-vault-aws-iam-server-id" in auth  # header participates in the signature
    assert "/us-east-2/sts/aws4_request" in auth


def test_vault_login_payload_requires_role():
    with pytest.raises(ValueError):
        vault.build_login_payload("", credentials=ReadOnlyCredentials("a", "b", None))


def test_vault_login_uses_injected_session_and_post():
    class Creds:
        def get_frozen_credentials(self):
            return ReadOnlyCredentials("AK", "SK", None)

    class Session:
        def get_credentials(self):
            return Creds()

    seen = {}

    def post(url, body):
        seen["url"], seen["body"] = url, body
        return {"auth": {"client_token": "hvs.x", "lease_duration": 900}}

    auth = vault.login("https://vault:8200/", "r", region="us-east-1", session=Session(), http_post=post)
    assert auth["client_token"] == "hvs.x"
    assert seen["url"] == "https://vault:8200/v1/auth/aws/login" and seen["body"]["role"] == "r"
    assert base64.b64decode(seen["body"]["iam_request_url"]).decode() == "https://sts.amazonaws.com/"


def test_askpass_prompt_classification():
    assert git_askpass.classify_prompt("Username for 'https://github.com': ") == "username"
    assert git_askpass.classify_prompt("Password for 'https://x-access-token@github.com': ") == "password"
    assert git_askpass.classify_prompt("???") == "unknown"


def test_askpass_broker_path_uses_lambda_invoke():
    class Lambda:
        def invoke(self, FunctionName, Payload):
            assert FunctionName == "arn:fn" and json.loads(Payload) == {"repositories": ["org/r"]}
            return {"Payload": io.BytesIO(json.dumps({"token": "ghs_abc"}).encode())}

    assert git_askpass.fetch_token_from_broker(function_arn="arn:fn", region="us-east-2", repos=["org/r"], client=Lambda()) == "ghs_abc"

    class Broken(Lambda):
        def invoke(self, FunctionName, Payload):
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorMessage":"x"}')}

    with pytest.raises(RuntimeError):
        git_askpass.fetch_token_from_broker(function_arn="arn:fn", region="us-east-2", client=Broken())


def test_askpass_identity_path_exchanges_workload_token():
    class Identity:
        def get_workload_access_token(self, workloadName):
            return {"workloadAccessToken": "wat"}

        def get_resource_api_key(self, workloadIdentityToken, resourceCredentialProviderName):
            assert workloadIdentityToken == "wat" and resourceCredentialProviderName == "gl"
            return {"apiKey": "glpat"}

        def get_resource_oauth2_token(self, **kwargs):
            assert kwargs["oauth2Flow"] == "USER_FEDERATION"
            return {"authorizationUrl": "https://idp/authorize"}  # pending consent

    assert git_askpass.fetch_token_from_identity(workload_name="w", provider="gl", region="r", client=Identity()) == "glpat"
    # inside Runtime: the platform-injected token is used directly, GetWorkloadAccessToken is never called
    class NoSelfServe(Identity):
        def get_workload_access_token(self, workloadName):
            raise AssertionError("must not self-serve when a workload token is provided")

    assert git_askpass.fetch_token_from_identity(workload_token="wat", provider="gl", region="r", client=NoSelfServe()) == "glpat"
    with pytest.raises(RuntimeError, match="WORKLOAD_TOKEN_FILE"):
        git_askpass.fetch_token_from_identity(provider="gl", region="r", client=Identity())
    with pytest.raises(RuntimeError, match="pending"):
        git_askpass.fetch_token_from_identity(workload_name="w", provider="gl", region="r", flow="user", client=Identity())


def test_read_workload_token_file(tmp_path):
    assert git_askpass.read_workload_token_file(str(tmp_path / "missing")) is None
    f = tmp_path / "wat"; f.write_text("  token-value \n")
    assert git_askpass.read_workload_token_file(str(f)) == "token-value"


def test_askpass_token_flag_prints_resolved_token(monkeypatch, capsys):
    monkeypatch.setattr(git_askpass, "resolve_token", lambda env: "ghs_short_lived")
    assert git_askpass.main(["git_askpass.py", "--token"]) == 0
    assert capsys.readouterr().out.strip() == "ghs_short_lived"


def _install_shim(tmp_path):
    """Copy gh_wrapper.sh next to a stub git_askpass.py and a fake real gh that dumps its environment."""
    import shutil
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "demo/secretless_auth/gh_wrapper.sh"
    shim_dir = tmp_path / "shim"; shim_dir.mkdir()
    shim = shim_dir / "gh"; shutil.copy(src, shim); shim.chmod(0o755)
    (shim_dir / "git_askpass.py").write_text("import sys; assert sys.argv[1] == '--token'; print('ghs_from_source')\n")
    fake_gh = tmp_path / "real_gh"
    fake_gh.write_text('#!/bin/sh\necho "args=$*"\necho "token=${GH_TOKEN:-unset}"\necho "cfg=${GH_CONFIG_DIR}"\n')
    fake_gh.chmod(0o755)
    return shim, fake_gh


def test_gh_shim_injects_token_only_into_child(tmp_path):
    import os
    import subprocess

    shim, fake_gh = _install_shim(tmp_path)
    env = {**os.environ, "GH_REAL": str(fake_gh), "GH_CONFIG_DIR": str(tmp_path / "cfg")}
    env.pop("GH_TOKEN", None)
    out = subprocess.run([str(shim), "pr", "list"], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "args=pr list" in out.stdout and "token=ghs_from_source" in out.stdout
    assert f"cfg={tmp_path / 'cfg'}" in out.stdout
    assert "GH_TOKEN" not in env  # the caller's environment never held the token


def test_gh_shim_blocks_auth_and_enforces_allowlist(tmp_path):
    import os
    import subprocess

    shim, fake_gh = _install_shim(tmp_path)
    env = {**os.environ, "GH_REAL": str(fake_gh), "GH_CONFIG_DIR": str(tmp_path / "cfg")}
    denied = subprocess.run([str(shim), "auth", "login"], env=env, capture_output=True, text=True, timeout=30)
    assert denied.returncode == 126 and "disabled" in denied.stderr
    env["GH_ALLOWED_SUBCOMMANDS"] = "pr issue"
    assert subprocess.run([str(shim), "repo", "delete"], env=env, capture_output=True, text=True, timeout=30).returncode == 126
    assert subprocess.run([str(shim), "issue", "list"], env=env, capture_output=True, text=True, timeout=30).returncode == 0


def test_broker_validate_request_enforces_allowlist():
    assert broker.validate_request({}, None) == {}
    assert broker.validate_request({"repositories": ["org/a"], "permissions": {"contents": "read"}}, {"org/a"}) == {
        "repositories": ["a"], "permissions": {"contents": "read"}}
    with pytest.raises(PermissionError):
        broker.validate_request({"repositories": ["org/evil"]}, {"org/a"})
    with pytest.raises(PermissionError):
        broker.validate_request({}, {"org/a"})  # allowlist demands explicit repos
    with pytest.raises(ValueError):
        broker.validate_request({"permissions": {"contents": 1}}, None)


def test_broker_jwt_claims():
    pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_key_bytes if False else key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    token = broker.build_app_jwt("12345", pem, now=1_000_000)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "12345"}
