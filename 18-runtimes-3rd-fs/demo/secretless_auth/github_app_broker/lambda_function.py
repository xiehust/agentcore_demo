"""GitHub App token broker (AWS Lambda).

Mints short-lived (1 hour) GitHub App *installation* tokens so the sandbox never
holds the App private key:

    sandbox (exec role) --lambda:InvokeFunction--> this Lambda
        1. read App private key from Secrets Manager (only the Lambda role can)
        2. sign a 10-minute RS256 JWT (iss = App id)
        3. POST /app/installations/{id}/access_tokens with optional
           `repositories` + `permissions` to narrow the token
        4. return {token, expires_at, repositories, permissions}

Access control: the Lambda resource policy / IAM allows only the AgentCore
runtime execution role(s) to invoke it; CloudTrail records every Invoke with
the caller identity, and the request payload shows which repos were requested.

Environment:
    GITHUB_APP_ID, GITHUB_INSTALLATION_ID, GITHUB_APP_KEY_SECRET_ARN,
    GITHUB_API_URL (default https://api.github.com; set for GHES)
    ALLOWED_REPOS (optional comma list; requests outside it are rejected)

Dependencies (Lambda layer or bundled): PyJWT[crypto]  (RS256 signing).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

import boto3

_secrets = boto3.client("secretsmanager")
_key_cache: dict[str, str] = {}


def _private_key() -> str:
    arn = os.environ["GITHUB_APP_KEY_SECRET_ARN"]
    if arn not in _key_cache:
        _key_cache[arn] = _secrets.get_secret_value(SecretId=arn)["SecretString"]
    return _key_cache[arn]


def build_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """RS256 JWT valid for 10 minutes (GitHub max), 60s clock-skew allowance."""
    import jwt  # PyJWT

    issued = (now or int(time.time())) - 60
    return jwt.encode({"iat": issued, "exp": issued + 600, "iss": app_id}, private_key_pem, algorithm="RS256")


def validate_request(body: dict[str, Any], allowed_repos: set[str] | None) -> dict[str, Any]:
    """Pure: normalise + authorise the caller's repository/permission narrowing."""
    repos = body.get("repositories") or []
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ValueError("repositories must be a list of 'owner/name' or 'name' strings")
    if allowed_repos is not None:
        bad = [r for r in repos if r not in allowed_repos and r.split("/")[-1] not in allowed_repos]
        if bad:
            raise PermissionError(f"repositories not allowed by broker policy: {bad}")
        if not repos:
            raise PermissionError("broker policy requires an explicit repositories list")
    permissions = body.get("permissions") or {}
    if not isinstance(permissions, dict) or not all(isinstance(v, str) for v in permissions.values()):
        raise ValueError("permissions must be a {scope: 'read'|'write'} object")
    request: dict[str, Any] = {}
    if repos:
        request["repositories"] = [r.split("/")[-1] for r in repos]  # API wants repo names only
    if permissions:
        request["permissions"] = permissions
    return request


def mint_installation_token(app_jwt: str, installation_id: str, request: dict[str, Any], api_url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/app/installations/{installation_id}/access_tokens",
        data=json.dumps(request).encode(),
        headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode())


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    body = event if isinstance(event, dict) else {}
    allowed_env = os.environ.get("ALLOWED_REPOS")
    allowed = {r.strip() for r in allowed_env.split(",") if r.strip()} if allowed_env else None
    try:
        request = validate_request(body, allowed)
    except (ValueError, PermissionError) as exc:
        return {"error": type(exc).__name__, "message": str(exc)}

    app_jwt = build_app_jwt(os.environ["GITHUB_APP_ID"], _private_key())
    result = mint_installation_token(app_jwt, os.environ["GITHUB_INSTALLATION_ID"], request,
                                     os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    return {
        "token": result["token"],
        "expires_at": result.get("expires_at"),
        "repositories": [r.get("full_name") for r in result.get("repositories", [])],
        "permissions": result.get("permissions"),
    }
