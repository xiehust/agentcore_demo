"""Shared helpers for the demo scripts: config/deployment loading, clients, session ids."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONFIG_PATH = REPO_ROOT / "config.json"
DEPLOYMENT_PATH = REPO_ROOT / "deployment.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.json missing — run `uv run python preflight.py` first.")
    return json.loads(CONFIG_PATH.read_text())


def load_deployment() -> dict:
    if not DEPLOYMENT_PATH.exists():
        raise FileNotFoundError(
            "deployment.json missing — run phase 3 deploy (agentcore deploy) first."
        )
    return json.loads(DEPLOYMENT_PATH.read_text())


def save_deployment(dep: dict) -> None:
    DEPLOYMENT_PATH.write_text(json.dumps(dep, indent=2) + "\n")


def region() -> str:
    try:
        return load_config().get("region", "us-west-2")
    except FileNotFoundError:
        return "us-west-2"


def control_client():
    return boto3.client("bedrock-agentcore-control", region_name=region())


def data_client():
    return boto3.client("bedrock-agentcore", region_name=region())


def new_session_id(prefix: str = "acme") -> str:
    """AgentCore runtime/harness session ids must be >= 33 chars."""
    sid = f"{prefix}-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"
    return sid  # e.g. 'acme-' (5) + 32 + 8 = 45 chars


def invoke_runtime(agent_runtime_arn: str, session_id: str, prompt: str) -> str:
    """Invoke a deployed AgentCore Runtime agent and return its text response."""
    client = data_client()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode(),
    )
    body = resp["response"].read()
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    # Entrypoint returns {"response": "..."}; unwrap if JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "response" in obj:
            return str(obj["response"])
        return json.dumps(obj)
    except (json.JSONDecodeError, ValueError):
        return text
