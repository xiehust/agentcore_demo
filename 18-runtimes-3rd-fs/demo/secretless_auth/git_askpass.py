#!/usr/bin/env python3
"""GIT_ASKPASS helper: hand git a short-lived token *per operation*, never store it.

git calls this program twice per HTTPS operation — once with a prompt containing
"Username", once with "Password". We answer with a fixed username and a token
fetched just-in-time from one of two secret-less sources:

  TOKEN_SOURCE=broker     invoke the GitHub App token-broker Lambda (see github_app_broker/)
                          with the runtime execution role -> 1h installation token
  TOKEN_SOURCE=identity   AgentCore Identity: GetWorkloadAccessToken -> GetResourceApiKey /
                          GetResourceOauth2Token for provider $IDENTITY_PROVIDER

Nothing is written to disk, the token is not in the remote URL, not in the
environment of the agent process, and git's own credential cache is disabled.

Setup inside the sandbox (once per session):

    export GIT_ASKPASS=/app/demo/secretless_auth/git_askpass.py GIT_TERMINAL_PROMPT=0
    git config --global credential.helper ''
    export TOKEN_SOURCE=broker BROKER_FUNCTION_ARN=arn:aws:lambda:...:function:github-app-token-broker
    export BROKER_REPOS=org/repo1,org/repo2      # optional: narrow the installation token
    git clone https://github.com/org/repo1

Environment used:
    TOKEN_SOURCE, GIT_USERNAME (default x-access-token for GitHub, oauth2 for GitLab),
    BROKER_FUNCTION_ARN, BROKER_REPOS, BROKER_PERMISSIONS (json),
    WORKLOAD_NAME, IDENTITY_PROVIDER, IDENTITY_FLOW (api_key|m2m|user), IDENTITY_SCOPES,
    AWS_REGION
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

def classify_prompt(prompt: str) -> str:
    """Return 'username' | 'password' | 'unknown' for a git askpass prompt."""
    lowered = (prompt or "").lower()
    if "username" in lowered:
        return "username"
    if "password" in lowered or "token" in lowered:
        return "password"
    return "unknown"


def fetch_token_from_broker(*, function_arn: str, region: str, repos: list[str] | None = None,
                            permissions: dict[str, str] | None = None, client: Any = None) -> str:
    """Invoke the token-broker Lambda with the execution role (SigV4). Returns installation token."""
    if client is None:
        import boto3

        client = boto3.client("lambda", region_name=region)
    payload: dict[str, Any] = {}
    if repos:
        payload["repositories"] = repos
    if permissions:
        payload["permissions"] = permissions
    response = client.invoke(FunctionName=function_arn, Payload=json.dumps(payload).encode())
    body = json.loads(response["Payload"].read())
    if response.get("FunctionError") or "token" not in body:
        raise RuntimeError(f"broker failed: {json.dumps(body)[:300]}")
    return body["token"]


DEFAULT_WORKLOAD_TOKEN_FILE = "/dev/shm/agentcore/workload_token"


def read_workload_token_file(path: str) -> str | None:
    """Per-request workload access token handed over by the agent process (see deploy/gh_shim_agent/agent.py).

    Inside AgentCore Runtime the platform injects the token into each request; the auto-created
    workload identity cannot be self-served via GetWorkloadAccessToken (ValidationException:
    "WorkloadIdentity is linked to a service"), so the agent writes it to a 0600 tmpfs file.
    """
    try:
        value = open(path, encoding="utf-8").read().strip()
    except OSError:
        return None
    return value or None


def fetch_token_from_identity(*, provider: str, region: str, flow: str = "api_key", workload_name: str | None = None,
                              workload_token: str | None = None, scopes: list[str] | None = None, client: Any = None) -> str:
    """AgentCore Identity: exchange the workload token for the provider's credential.

    Prefer the platform-injected ``workload_token``; fall back to GetWorkloadAccessToken(workload_name)
    for workloads hosted outside Runtime (ECS/EC2) with an explicitly created workload identity.
    """
    if client is None:
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=region)
    if not workload_token:
        if not workload_name:
            raise RuntimeError("need WORKLOAD_TOKEN_FILE (inside Runtime) or WORKLOAD_NAME (outside Runtime)")
        workload_token = client.get_workload_access_token(workloadName=workload_name)["workloadAccessToken"]
    if flow == "api_key":
        return client.get_resource_api_key(workloadIdentityToken=workload_token,
                                           resourceCredentialProviderName=provider)["apiKey"]
    oauth_flow = "M2M" if flow == "m2m" else "USER_FEDERATION"
    response = client.get_resource_oauth2_token(workloadIdentityToken=workload_token,
                                                resourceCredentialProviderName=provider,
                                                scopes=scopes or [], oauth2Flow=oauth_flow)
    token = response.get("accessToken")
    if not token:
        raise RuntimeError("no access token yet; user authorization may be pending: "
                           + str(response.get("authorizationUrl", ""))[:200])
    return token


def resolve_token(env: dict[str, str]) -> str:
    source = env.get("TOKEN_SOURCE", "broker")
    region = env.get("AWS_REGION", "us-east-1")
    if source == "broker":
        repos = [r for r in env.get("BROKER_REPOS", "").split(",") if r]
        permissions = json.loads(env["BROKER_PERMISSIONS"]) if env.get("BROKER_PERMISSIONS") else None
        return fetch_token_from_broker(function_arn=env["BROKER_FUNCTION_ARN"], region=region, repos=repos,
                                       permissions=permissions)
    if source == "identity":
        scopes = [s for s in env.get("IDENTITY_SCOPES", "").split(",") if s]
        return fetch_token_from_identity(
            provider=env["IDENTITY_PROVIDER"], region=region, flow=env.get("IDENTITY_FLOW", "api_key"), scopes=scopes,
            workload_token=read_workload_token_file(env.get("WORKLOAD_TOKEN_FILE", DEFAULT_WORKLOAD_TOKEN_FILE)),
            workload_name=env.get("WORKLOAD_NAME"),
        )
    raise RuntimeError(f"unknown TOKEN_SOURCE {source!r}")


def main(argv: list[str]) -> int:
    prompt = argv[1] if len(argv) > 1 else ""
    if prompt == "--token":  # used by gh_wrapper.sh: print a fresh short-lived token and exit
        print(resolve_token(dict(os.environ)))
        return 0
    kind = classify_prompt(prompt)
    if kind == "username":
        print(os.environ.get("GIT_USERNAME", "x-access-token"))
        return 0
    if kind == "password":
        print(resolve_token(dict(os.environ)))
        return 0
    sys.stderr.write(f"git_askpass: unrecognised prompt {prompt!r}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
