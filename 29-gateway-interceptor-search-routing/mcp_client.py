"""Minimal SigV4-signed MCP client for the PoC gateway (used by the test scripts)."""

import json
from pathlib import Path

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-1"
state = json.loads((Path(__file__).parent / "state.json").read_text())
URL = state["gateway_url"]
creds = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()
_id = 0


def rpc(method, params=None):
    global _id
    _id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}})
    req = AWSRequest("POST", URL, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(req)
    r = requests.post(URL, data=body, headers=dict(req.headers), timeout=60)
    r.raise_for_status()  # JSON-RPC errors (e.g. -32602) still arrive as HTTP 200
    text = r.text
    if text.startswith("event:") or "data:" in text[:50]:
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return r.status_code, json.loads(text)


def call(name, args):
    code, resp = rpc("tools/call", {"name": name, "arguments": args})
    content = (resp.get("result") or {}).get("content") or []
    text = content[0].get("text", "") if content else json.dumps(resp, ensure_ascii=False)
    return code, resp, text
