#!/usr/bin/env python3
"""
Minimal MCP client for an AgentCore Gateway using AWS_IAM inbound auth.

Signs every JSON-RPC request with SigV4 against the `bedrock-agentcore` service,
so no Cognito / OAuth IdP is needed to exercise the gateway. Handles both plain
JSON and text/event-stream (SSE) responses.

Usage:
  python mcp_client.py list
  python mcp_client.py call <tool_name> '{"json":"args"}'
"""

import json
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def load_state(path="state.env"):
    state = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                state[k] = v
    return state


STATE = load_state()
URL = STATE["GW_URL"]
REGION = URL.split(".")[-3]
SESSION = boto3.Session()
CREDS = SESSION.get_credentials().get_frozen_credentials()

_rpc_id = 0


def rpc(method, params=None, session_id=None):
    """Send one signed JSON-RPC request; return (result, response_headers)."""
    global _rpc_id
    _rpc_id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _rpc_id,
                       "method": method, "params": params or {}})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # SigV4-sign the exact body + headers we are about to send.
    aws_req = AWSRequest(method="POST", url=URL, data=body.encode(), headers=headers)
    SigV4Auth(CREDS, "bedrock-agentcore", REGION).add_auth(aws_req)

    req = urllib.request.Request(URL, data=body.encode(),
                                 headers=dict(aws_req.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
            resp_headers = dict(resp.headers)
    except urllib.error.HTTPError as err:
        print(f"HTTP {err.code}: {err.read().decode()}", file=sys.stderr)
        raise

    # An SSE response frames the payload in "data:" lines.
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break

    payload = json.loads(raw)
    if "error" in payload:
        raise RuntimeError(f"MCP error: {payload['error']}")
    return payload.get("result"), resp_headers


def connect():
    """Run the MCP initialize handshake; return the session id, if any."""
    _, headers = rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "noegress-verifier", "version": "1.0"},
    })
    return headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    sid = connect()
    cmd = sys.argv[1]

    if cmd == "list":
        result, _ = rpc("tools/list", {}, sid)
        for tool in result["tools"]:
            print(f"- {tool['name']}: {tool.get('description','')}")
        return 0

    if cmd == "call":
        name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        result, _ = rpc("tools/call", {"name": name, "arguments": args}, sid)
        for item in result.get("content", []):
            text = item.get("text", "")
            try:
                print(json.dumps(json.loads(text), indent=2))
            except (ValueError, TypeError):
                print(text)
        if result.get("isError"):
            print("(tool reported isError=true)", file=sys.stderr)
            return 2
        return 0

    print(f"unknown command {cmd!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
