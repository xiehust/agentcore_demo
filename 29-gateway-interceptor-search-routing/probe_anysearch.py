"""Probe the AnySearch MCP server directly: initialize, tools/list, one search.

Key comes from stdin so it never lands in a file or argv:
    printf %s "$ANYSEARCH_KEY" | .venv/bin/python probe_anysearch.py ["query"]
"""

import json
import sys
import urllib.request

URL = "https://api.anysearch.com/mcp"
key = sys.stdin.read().strip()
query = sys.argv[1] if len(sys.argv) > 1 else "杭州西湖 最佳游览季节"
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
        "X-Anysearch-Client": "mcp/1.0.0",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        session = r.headers.get("Mcp-Session-Id") or session
        raw = r.read().decode()
    if notify:
        return None
    for line in raw.splitlines():  # SSE or plain JSON
        if line.startswith("data:"):
            raw = line[5:].strip()
    return json.loads(raw)


print(json.dumps(rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "probe", "version": "0"}}), ensure_ascii=False)[:600])
rpc("notifications/initialized", notify=True)
tools = rpc("tools/list", {}, rid=2)
for t in tools["result"]["tools"]:
    print(t["name"], json.dumps(t["inputSchema"], ensure_ascii=False))
if len(sys.argv) > 2:
    res = rpc("tools/call", {"name": sys.argv[2], "arguments": json.loads(sys.argv[3])}, rid=3)
    print(json.dumps(res, ensure_ascii=False, indent=1)[:5000])
