"""Only one search tool is exposed, hidden tools can't be called, routing still works."""

import json
import sys
from pathlib import Path

from test import call, rpc

EXPOSED = "websearch___WebSearch"
CN = json.loads((Path(__file__).parent / "state.json").read_text()).get("cn_provider", "exa")
HIDDEN = ["exa___web_search_exa", "exa___agent_run", "mockexa___web_search_exa",
          "anysearch___search", "anysearch___extract", "anysearch___score"]
results = []


def provider_of(text):
    try:
        return json.loads(text).get("provider")
    except ValueError:
        return None


def looks_like_websearch(text):
    return provider_of(text) == "agentcore-web-search"


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


code, tl = rpc("tools/list")
names = [t["name"] for t in tl.get("result", {}).get("tools", [])]
check("tools/list exposes only the search tool", names == [EXPOSED], f"http={code} tools={names}")

for hidden in HIDDEN:
    code, resp, text = call(hidden, {"query": "x", "objective": "x"})
    err = resp.get("error") or {}
    check(f"direct call to {hidden} is rejected", "Unknown tool" in (err.get("message") or ""), f"http={code} error={err}")

code, resp, text = call(EXPOSED, {"query": "Amazon Bedrock AgentCore Gateway interceptor", "maxResults": 2})
check("English -> AgentCore Web Search", looks_like_websearch(text) and "error" not in resp, text[:120].replace("\n", " "))

code, resp, text = call(EXPOSED, {"query": "杭州西湖 最佳游览季节", "maxResults": 2})
check(f"Chinese -> {CN}", provider_of(text) == CN, text[:120].replace("\n", " "))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
