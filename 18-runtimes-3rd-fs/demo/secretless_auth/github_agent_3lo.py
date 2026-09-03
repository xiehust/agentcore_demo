#!/usr/bin/env python3
"""GitHub access on behalf of the end user with AgentCore Identity (3LO / USER_FEDERATION).

Follows the official sample
https://github.com/awslabs/agentcore-samples/tree/main/01-features/05-authenticate-and-authorize/02-outbound-auth/03-outbound-auth-github
and adds the pieces this research needs: the same token also drives ``git`` via
``GIT_ASKPASS`` so clone/push works without any credential on disk.

Flow (nothing static lives in the sandbox):

    setup (operator, once)     create_oauth2_credential_provider(GithubOauth2, clientId/clientSecret)
                               -> callbackUrl  ==> paste into the GitHub OAuth App "Authorization callback URL"
    first tool call            @requires_access_token(auth_flow="USER_FEDERATION") -> no token yet
                               -> on_auth_url(url) streams the GitHub consent URL to the user
    user consents              GitHub -> AgentCore callback -> your callback server calls CompleteResourceTokenAuth
                               (binds the GitHub token to the signed-in user; see oauth2_callback_server.py in the sample)
    next tool call             decorator returns the cached GitHub token; refresh handled by Identity

Runtime environment: CALLBACK_URL (your OAuth2 callback server URL used for session binding),
GITHUB_PROVIDER (default github-provider), MODEL_ID.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import tempfile
from typing import Optional

import httpx
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

app = BedrockAgentCoreApp()
PROVIDER = os.environ.get("GITHUB_PROVIDER", "github-provider")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
_auth_urls: asyncio.Queue[str] = asyncio.Queue()


async def on_auth_url(url: str) -> None:
    """Identity calls this when the user still has to consent; surface the URL to the caller."""
    app.logger.info("GitHub authorization required: %s", url)
    await _auth_urls.put(url)


def _with_github_token(fn):
    """Nest the decorator inside the tool so `access_token` is not part of the tool schema (as in the sample)."""
    return requires_access_token(
        provider_name=PROVIDER,
        scopes=["repo", "read:user"],
        auth_flow="USER_FEDERATION",
        on_auth_url=on_auth_url,
        force_authentication=False,
        callback_url=CALLBACK_URL,
    )(fn)


@tool
def list_private_repos() -> str:
    """List the signed-in user's private GitHub repositories."""

    @_with_github_token
    def _run(access_token: Optional[str] = None) -> str:
        if not access_token:
            return json.dumps({"auth_required": True, "message": "Open the authorization URL, then ask again."})
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
        with httpx.Client(timeout=15) as client:
            repos = client.get("https://api.github.com/user/repos", params={"type": "private", "per_page": 50}, headers=headers)
            repos.raise_for_status()
        return "\n".join(f"{r['full_name']} ({r.get('language') or '-'})" for r in repos.json()) or "no private repositories"

    return _run()


@tool
def clone_repo(full_name: str) -> str:
    """Clone a repository (owner/name) into the workspace using the user's GitHub token, without storing it."""

    @_with_github_token
    def _run(access_token: Optional[str] = None) -> str:
        if not access_token:
            return json.dumps({"auth_required": True})
        workspace = os.environ.get("WORKSPACE_ROOT", "/mnt/workspace")
        dest = os.path.join(workspace, full_name.split("/")[-1])
        # GIT_ASKPASS: git asks this helper for username/password at run time; the token never touches
        # the remote URL, git config, or the filesystem.
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as helper:
            helper.write('#!/bin/sh\ncase "$1" in *sername*) echo x-access-token;; *) printf %s "$GH_TOKEN";; esac\n')
        os.chmod(helper.name, 0o700)
        env = {**os.environ, "GIT_ASKPASS": helper.name, "GIT_TERMINAL_PROMPT": "0", "GH_TOKEN": access_token}
        try:
            proc = subprocess.run(["git", "-c", "credential.helper=", "clone", "--depth", "1",
                                   f"https://github.com/{full_name}.git", dest],
                                  env=env, capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(helper.name)
        return f"cloned to {dest}" if proc.returncode == 0 else f"git failed: {proc.stderr[-400:]}"

    return _run()  # full_name is captured from the enclosing tool call


GH_ALLOWED = {"pr", "issue", "repo", "api", "search", "release", "run", "workflow"}  # never "auth"


@tool
def gh(args: str) -> str:
    """Run a GitHub CLI command with the user's token, e.g. args="pr list --repo org/repo --state open".

    The token is passed only to the gh child process (GH_TOKEN); it is not stored by gh and
    never enters this agent's own environment.
    """
    argv = shlex.split(args)
    if not argv or argv[0] not in GH_ALLOWED:
        return f"gh: subcommand not allowed; use one of {sorted(GH_ALLOWED)}"

    @_with_github_token
    def _run(access_token: Optional[str] = None) -> str:
        if not access_token:
            return json.dumps({"auth_required": True})
        env = {**os.environ, "GH_TOKEN": access_token, "GH_PROMPT_DISABLED": "1",
               "GH_NO_UPDATE_NOTIFIER": "1", "GH_CONFIG_DIR": "/dev/shm/gh-config"}  # tmpfs, nothing persists
        proc = subprocess.run(["gh", *argv], env=env, capture_output=True, text=True, timeout=120)
        return proc.stdout[-4000:] if proc.returncode == 0 else f"gh failed ({proc.returncode}): {proc.stderr[-800:]}"

    return _run()


agent = Agent(
    model=os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    tools=[list_private_repos, clone_repo, gh],
    system_prompt="You are a GitHub assistant. Tools handle authentication; if a tool reports auth_required, "
                  "tell the user to open the authorization link and try again.",
)


@app.entrypoint
async def invoke(payload: dict, context=None):
    response = await agent.invoke_async(payload.get("prompt", "List my private repositories."))
    urls = []
    while not _auth_urls.empty():
        urls.append(_auth_urls.get_nowait())
    return {"result": str(response), "authorization_urls": urls}


if __name__ == "__main__":
    app.run()
