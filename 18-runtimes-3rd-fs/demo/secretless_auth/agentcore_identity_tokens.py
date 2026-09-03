#!/usr/bin/env python3
"""AgentCore Identity: credential providers + in-runtime token retrieval (layer B in docs/02).

Control plane (run by an operator, once):
    python3 agentcore_identity_tokens.py create-gitlab-provider --name gitlab \
        --discovery-url https://gitlab.example.com/.well-known/openid-configuration \
        --client-id ... --client-secret-file ./client_secret.txt
    python3 agentcore_identity_tokens.py create-github-provider --name github --client-id ... --client-secret-file ...
    python3 agentcore_identity_tokens.py create-api-key-provider --name gitlab-group-token --api-key-file ./token.txt

The secret is read from a file so it never appears in shell history or LLM
context; after creation it lives only in the Token Vault (Secrets Manager + KMS).

Data plane (inside the Runtime — the decorators pick up the workload identity
automatically):

    from bedrock_agentcore.identity.auth import requires_access_token, requires_api_key

    @requires_access_token(provider_name="gitlab", scopes=["read_api"], auth_flow="USER_FEDERATION",
                           on_auth_url=lambda url: print("authorize:", url))
    async def list_gitlab_projects(*, access_token: str) -> list: ...

    @requires_api_key(provider_name="gitlab-group-token")
    async def gitlab_ci_status(*, api_key: str) -> dict: ...

`gitlab_user` / `gitlab_projects` below are complete examples; run them from an
agent tool or the entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------------------- control plane
def _read_secret_file(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{path} is empty")
    return value


def create_gitlab_provider(client: Any, *, name: str, discovery_url: str, client_id: str, client_secret: str,
                           private_endpoint: str | None = None) -> dict[str, Any]:
    """GitLab exposes OIDC discovery; use CustomOauth2. Self-hosted GitLab inside a VPC -> privateEndpoint."""
    config: dict[str, Any] = {
        "oauthDiscovery": {"discoveryUrl": discovery_url},
        "clientId": client_id,
        "clientSecret": client_secret,
    }
    if private_endpoint:
        config["privateEndpoint"] = private_endpoint
    return client.create_oauth2_credential_provider(
        name=name, credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={"customOauth2ProviderConfig": config},
    )


def create_github_provider(client: Any, *, name: str, client_id: str, client_secret: str) -> dict[str, Any]:
    return client.create_oauth2_credential_provider(
        name=name, credentialProviderVendor="GithubOauth2",
        oauth2ProviderConfigInput={"githubOauth2ProviderConfig": {"clientId": client_id, "clientSecret": client_secret}},
    )


def create_api_key_provider(client: Any, *, name: str, api_key: str) -> dict[str, Any]:
    """For GitLab group/project access tokens (GitLab has no client_credentials grant)."""
    return client.create_api_key_credential_provider(name=name, apiKey=api_key)


# ----------------------------------------------------------------------------- data plane
def _get(url: str, token: str, header: str = "Authorization", prefix: str = "Bearer ") -> Any:
    req = urllib.request.Request(url, headers={header: f"{prefix}{token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode())


def make_gitlab_tools(gitlab_url: str, oauth_provider: str, api_key_provider: str):
    """Return (gitlab_user, gitlab_projects) coroutines bound to provider names.

    Built lazily so this module imports without the AgentCore SDK installed.
    """
    from bedrock_agentcore.identity.auth import requires_access_token, requires_api_key

    @requires_access_token(provider_name=oauth_provider, scopes=["read_user", "read_api"], auth_flow="USER_FEDERATION",
                           on_auth_url=lambda url: print(f"Please authorize: {url}"), force_authentication=False)
    async def gitlab_user(*, access_token: str) -> dict[str, Any]:
        # On behalf of the signed-in end user (3LO). Token is 2h, refresh handled by Identity.
        return _get(f"{gitlab_url}/api/v4/user", access_token)

    @requires_api_key(provider_name=api_key_provider)
    async def gitlab_projects(*, api_key: str) -> list[dict[str, Any]]:
        # Service identity via group/project access token stored in the Token Vault.
        return _get(f"{gitlab_url}/api/v4/projects?membership=true&simple=true", api_key, header="PRIVATE-TOKEN", prefix="")

    return gitlab_user, gitlab_projects


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default="us-east-1")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("create-gitlab-provider")
    g.add_argument("--name", required=True); g.add_argument("--discovery-url", required=True)
    g.add_argument("--client-id", required=True); g.add_argument("--client-secret-file", required=True)
    g.add_argument("--private-endpoint", default=None, help="for self-hosted GitLab reachable only inside the VPC")

    h = sub.add_parser("create-github-provider")
    h.add_argument("--name", required=True); h.add_argument("--client-id", required=True)
    h.add_argument("--client-secret-file", required=True)

    k = sub.add_parser("create-api-key-provider")
    k.add_argument("--name", required=True); k.add_argument("--api-key-file", required=True)

    t = sub.add_parser("try-gitlab", help="run inside the Runtime (needs workload identity)")
    t.add_argument("--gitlab-url", required=True); t.add_argument("--oauth-provider", required=True)
    t.add_argument("--api-key-provider", required=True)

    a = p.parse_args()
    import boto3

    if a.cmd == "try-gitlab":
        user, projects = make_gitlab_tools(a.gitlab_url, a.oauth_provider, a.api_key_provider)
        print(json.dumps({"projects": asyncio.run(projects())[:3]}, indent=2, default=str))
        print(json.dumps({"user": asyncio.run(user())}, indent=2, default=str))
        return 0

    control = boto3.client("bedrock-agentcore-control", region_name=a.region)
    if a.cmd == "create-gitlab-provider":
        out = create_gitlab_provider(control, name=a.name, discovery_url=a.discovery_url, client_id=a.client_id,
                                     client_secret=_read_secret_file(a.client_secret_file), private_endpoint=a.private_endpoint)
    elif a.cmd == "create-github-provider":
        out = create_github_provider(control, name=a.name, client_id=a.client_id,
                                     client_secret=_read_secret_file(a.client_secret_file))
    else:
        out = create_api_key_provider(control, name=a.name, api_key=_read_secret_file(a.api_key_file))
    out.pop("ResponseMetadata", None)
    print(json.dumps(out, indent=2, default=str))  # contains callbackUrl for OAuth providers -> register with IdP
    return 0


if __name__ == "__main__":
    sys.exit(main())
