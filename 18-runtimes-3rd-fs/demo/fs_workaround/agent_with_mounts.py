#!/usr/bin/env python3
"""Strands agent on AgentCore Runtime that uses native mounts + userspace sync.

Deploy with ``scripts/02-create-runtime-with-fs.sh`` so that:

    /mnt/workspace  -> sessionStorage   (per-session, survives stop/resume, <= 1 GB)
    /mnt/shared     -> efsAccessPoint   (shared across sessions/agents, full POSIX)   [VPC]
    /mnt/datasets   -> s3FilesAccessPoint (read-mostly datasets, also visible via S3 API) [VPC]

Anything that is *not* EFS/S3 Files (a third-party object store, SFTP, ...) is
pulled into ``/mnt/workspace/<project>`` at session start and pushed back after
each turn with ``s3_workspace_sync`` — no FUSE required.

Environment:
    WORKSPACE_ROOT   default /mnt/workspace
    REMOTE_S3_URL    optional s3://bucket/prefix to sync (S3-compatible endpoint via S3_ENDPOINT_URL)
    MODEL_ID         Bedrock model id for the agent
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands_tools import file_read, file_write, shell

from s3_workspace_sync import S3Adapter, pull, push

app = BedrockAgentCoreApp()

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/mnt/workspace"))
REMOTE_S3_URL = os.environ.get("REMOTE_S3_URL", "")
PROJECT_DIR = WORKSPACE_ROOT / "project"


def _adapter() -> S3Adapter | None:
    if not REMOTE_S3_URL.startswith("s3://"):
        return None
    bucket, _, prefix = REMOTE_S3_URL[5:].partition("/")
    return S3Adapter(bucket, prefix, endpoint_url=os.environ.get("S3_ENDPOINT_URL"), region=os.environ.get("AWS_REGION"))


@tool
def mount_status() -> str:
    """Report which mount paths exist and how much space they have."""
    report = {}
    for mount in ("/mnt/workspace", "/mnt/shared", "/mnt/datasets"):
        path = Path(mount)
        if path.exists():
            usage = os.statvfs(mount)
            report[mount] = {"free_mb": round(usage.f_bavail * usage.f_frsize / 1e6, 1)}
        else:
            report[mount] = "not mounted"
    return json.dumps(report)


@tool
def sync_workspace(direction: str = "push") -> str:
    """Pull the remote project into the workspace or push local changes back. direction: pull|push."""
    adapter = _adapter()
    if adapter is None:
        return "REMOTE_S3_URL not configured; workspace is local-only (sessionStorage)."
    stats = pull(adapter, PROJECT_DIR) if direction == "pull" else push(adapter, PROJECT_DIR, conflict="local_wins")
    return json.dumps(stats)


agent = Agent(
    model=os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    system_prompt=(
        f"You are a coding assistant. Work inside {PROJECT_DIR}. "
        "Shared read-write files live under /mnt/shared, datasets under /mnt/datasets."
    ),
    tools=[file_read, file_write, shell, mount_status, sync_workspace],
)

_pulled = False


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    global _pulled
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    adapter = _adapter()
    if adapter is not None and not _pulled:
        pull(adapter, PROJECT_DIR)  # first turn of this compute: hydrate from remote
        _pulled = True
    result = agent(payload.get("prompt", "Summarise the project."))
    if adapter is not None:
        push(adapter, PROJECT_DIR, conflict="local_wins")  # checkpoint every turn
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
