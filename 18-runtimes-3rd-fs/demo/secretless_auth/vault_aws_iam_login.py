#!/usr/bin/env python3
"""Log in to HashiCorp Vault from an AgentCore sandbox with ZERO Vault secrets.

Vault's AWS auth method (type ``iam``) trusts a SigV4-signed
``sts:GetCallerIdentity`` request. Inside AgentCore Runtime the signing
credentials are the execution role's temporary credentials injected by the
platform, so nothing static ever lives in the sandbox. Vault verifies the
signature by forwarding the request to STS and maps the caller ARN to a Vault
role (``bound_iam_principal_arn``), returning a short-TTL Vault token.

    Runtime (exec role creds) --SigV4(GetCallerIdentity)--> Vault /v1/auth/aws/login --> STS
                                                                    |
                                                        Vault token (TTL 15m) -> dynamic secrets

``build_login_payload`` is a pure function (unit-tested); only ``login``/``read``
touch the network.

Usage:
    python3 vault_aws_iam_login.py --vault-addr https://vault.internal:8200 --role agentcore-runtime \
        [--server-id vault.internal] [--read secret/data/demo]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any

STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def _b64(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.b64encode(raw).decode("ascii")


def build_login_payload(role: str, *, credentials: Any, region: str = "us-east-1",
                        server_id: str | None = None, sts_endpoint: str | None = None) -> dict[str, str]:
    """Sign sts:GetCallerIdentity with botocore and shape Vault's login body.

    ``credentials`` is a botocore ``Credentials``/``ReadOnlyCredentials`` object
    (access key, secret key, optional token). Vault requires the four request
    components (method, URL, body, headers) so it can replay the signed request.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    if not role:
        raise ValueError("role is required")
    url = sts_endpoint or (f"https://sts.{region}.amazonaws.com/" if region != "us-east-1" else "https://sts.amazonaws.com/")
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    if server_id:
        # Must be part of the signed headers; Vault checks it against iam_server_id_header_value.
        headers["X-Vault-AWS-IAM-Server-ID"] = server_id
    request = AWSRequest(method="POST", url=url, data=STS_BODY, headers=headers)
    SigV4Auth(credentials, "sts", region).add_auth(request)
    signed_headers = {k: [v] for k, v in request.headers.items()}
    return {
        "role": role,
        "iam_http_request_method": "POST",
        "iam_request_url": _b64(url),
        "iam_request_body": _b64(STS_BODY),
        "iam_request_headers": _b64(json.dumps(signed_headers)),
    }


def login(vault_addr: str, role: str, *, region: str, server_id: str | None = None,
          mount: str = "aws", session: Any = None, http_post: Any = None) -> dict[str, Any]:
    """Return Vault's ``auth`` block ({client_token, lease_duration, policies, ...})."""
    import urllib.request

    if session is None:
        import boto3

        session = boto3.session.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials available (inside AgentCore Runtime the execution role provides them)")
    payload = build_login_payload(role, credentials=credentials.get_frozen_credentials(), region=region, server_id=server_id)

    def _default_post(url: str, body: dict[str, str]) -> dict[str, Any]:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310 - Vault URL is operator-provided
            return json.loads(response.read().decode())

    post = http_post or _default_post
    response = post(f"{vault_addr.rstrip('/')}/v1/auth/{mount}/login", payload)
    auth = response.get("auth")
    if not isinstance(auth, dict) or not auth.get("client_token"):
        raise RuntimeError(f"Vault login did not return a token: {json.dumps(response)[:300]}")
    return auth


def read_secret(vault_addr: str, token: str, path: str) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(f"{vault_addr.rstrip('/')}/v1/{path.lstrip('/')}", headers={"X-Vault-Token": token})
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--vault-addr", required=True)
    p.add_argument("--role", required=True, help="Vault role with auth_type=iam and bound_iam_principal_arn = runtime execution role")
    p.add_argument("--region", default="us-east-1", help="STS region to sign for (Vault must accept it)")
    p.add_argument("--server-id", default=None, help="value for X-Vault-AWS-IAM-Server-ID (recommended)")
    p.add_argument("--mount", default="aws")
    p.add_argument("--read", default=None, help="optional secret path to read, e.g. secret/data/demo")
    a = p.parse_args()
    auth = login(a.vault_addr, a.role, region=a.region, server_id=a.server_id, mount=a.mount)
    summary = {"lease_duration": auth.get("lease_duration"), "policies": auth.get("policies"),
               "token_prefix": str(auth["client_token"])[:6] + "..."}
    if a.read:
        summary["secret"] = read_secret(a.vault_addr, auth["client_token"], a.read).get("data")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
