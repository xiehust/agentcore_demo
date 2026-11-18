"""Probe a Bearer-auth MCP server directly: initialize, tools/list, optionally one tools/call.

Key comes from stdin so it never lands in a file or argv:
    printf %s "<key>" | .venv/bin/python probe_mcp.py <url> [tool '<json args>']
"""

import json
import sys
import urllib.request

url = sys.argv[1]
key = sys.stdin.read().strip()
session = None


def rpc(method, params=None, rid=1, notify=False):
    global session
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        session = r.headers.get("Mcp-Session-Id") or session
        raw = r.read().decode()
    if notify:
        return None
    for line in raw.splitlines():  # SSE or plain JSON
        if line.startswith("data:"):
            raw = line[5:].strip()
    return json.loads(raw)


init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                          "clientInfo": {"name": "probe", "version": "0"}})
print(json.dumps(init, ensure_ascii=False)[:400])
rpc("notifications/initialized", notify=True)
for t in rpc("tools/list", {}, rid=2)["result"]["tools"]:
    print(t["name"], json.dumps(t["inputSchema"], ensure_ascii=False))
if len(sys.argv) > 3:
    res = rpc("tools/call", {"name": sys.argv[2], "arguments": json.loads(sys.argv[3])}, rid=3)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:5000])
