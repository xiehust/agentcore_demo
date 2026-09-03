#!/usr/bin/env python3
"""Strands agent on AgentCore Runtime that uses the GitHub CLI through the gh shim.

The agent itself knows nothing about credentials: its only tool runs `gh ...` and
relies on PATH resolving to /opt/shim/gh, which fetches a short-lived token from
AgentCore Identity (API key provider) and injects it into the gh child process only.

Payloads:
  {"prompt": "..."}   normal agent turn (model decides when to call the gh tool)
  {"verify": true}    deterministic evidence about the shim, no model call
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from starlette.middleware.base import BaseHTTPMiddleware
from strands import Agent, tool

app = BedrockAgentCoreApp()
WORKLOAD_TOKEN_FILE = Path(os.environ.get("WORKLOAD_TOKEN_FILE", "/dev/shm/agentcore/workload_token"))
LAST_REQUEST_HEADER_NAMES: list[str] = []  # diagnostics only: header *names* the platform sent (never values)


class _RecordHeaderNames(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/invocations":
            LAST_REQUEST_HEADER_NAMES[:] = sorted(request.headers.keys())
        return await call_next(request)


app.add_middleware(_RecordHeaderNames)


def hand_over_workload_token() -> bool:
    """Make the platform-injected workload access token available to the shim (a separate process).

    The auto-created Runtime workload identity cannot be self-served with GetWorkloadAccessToken,
    so the per-request token from the SDK context is written to a 0600 file on tmpfs. This is the
    agent's *identity* token, not the GitHub credential; it is only useful together with the
    execution role, which any process in this microVM already has.
    """
    token = BedrockAgentCoreContext.get_workload_access_token()
    if not token:
        return False
    WORKLOAD_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = WORKLOAD_TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(WORKLOAD_TOKEN_FILE)
    return True


def _run(argv: list[str], env: dict | None = None, timeout: int = 60) -> dict:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    return {"argv": argv, "rc": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-800:]}


@tool
def run_gh(args: str) -> str:
    """Run a GitHub CLI command, e.g. args="api /user --jq .login" or "repo list --limit 5".

    Authentication is handled outside this agent; never pass tokens.
    """
    result = _run(["gh", *shlex.split(args)])  # PATH -> /opt/shim/gh
    return json.dumps({"rc": result["rc"], "stdout": result["stdout"], "stderr": result["stderr"]})


agent = Agent(
    model=os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    tools=[run_gh],
    system_prompt=(
        "You are a GitHub assistant running in a sandbox. Use run_gh for anything GitHub-related; "
        "authentication is already handled, never ask for or mention tokens. Answer concisely."
    ),
)


def verify() -> dict:
    """Deterministic evidence, collected from inside the agent process."""
    gh_config_home = Path.home() / ".config" / "gh" / "hosts.yml"
    injection = _run(["gh", "api", "/rate_limit"], env={**os.environ, "GH_REAL": "/opt/shim/print_token_env.sh"})
    return {
        "which_gh": shutil.which("gh"),
        "workload_token_handed_over": WORKLOAD_TOKEN_FILE.exists() and oct(WORKLOAD_TOKEN_FILE.stat().st_mode & 0o777) == "0o600",
        "workload_token_in_context": BedrockAgentCoreContext.get_workload_access_token() is not None,
        "request_header_names": list(LAST_REQUEST_HEADER_NAMES),
        "agent_env_has_GH_TOKEN": "GH_TOKEN" in os.environ or "GITHUB_TOKEN" in os.environ,
        "gh_hosts_yml_exists": gh_config_home.exists(),
        "gh_config_dir_files": sorted(p.name for p in Path("/dev/shm/gh-config").glob("*")) if Path("/dev/shm/gh-config").exists() else [],
        "shim_injects_token_into_child": injection,
        "gh_api_user": _run(["gh", "api", "/user", "--jq", ".login"]),
        "gh_repo_list": _run(["gh", "repo", "list", "--limit", "3", "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"]),
        "gh_auth_login_blocked": _run(["gh", "auth", "login", "--with-token"]),
        "gh_disallowed_subcommand_blocked": _run(["gh", "release", "list"]),
        "agent_env_after": "GH_TOKEN" in os.environ,
    }


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    hand_over_workload_token()  # every request: the shim needs this request's workload token
    if payload.get("verify"):
        return {"verify": verify()}
    prompt = payload.get("prompt", "Who am I on GitHub and what are my 3 most recent repositories?")
    result = agent(prompt)
    return {
        "result": str(result),
        "agent_env_has_GH_TOKEN": "GH_TOKEN" in os.environ,
        "tool_calls": sum(1 for m in agent.messages for c in m.get("content", []) if isinstance(c, dict) and "toolUse" in c),
    }


if __name__ == "__main__":
    app.run()
