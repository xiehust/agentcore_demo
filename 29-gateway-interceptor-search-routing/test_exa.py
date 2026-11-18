"""Real-Exa routing test. The interceptor's DST_TOOL env must point at the Exa tool."""

import sys

from test import call, rpc  # reuses SigV4 MCP client; importing runs nothing heavy

EXA_TOOL = "exa___web_search_exa"


def looks_like_websearch(text):
    return '"search_queries_hash"' in text or (text.startswith('{"id"') and '"results"' in text)


code, tl = rpc("tools/list")
names = [t["name"] for t in tl.get("result", {}).get("tools", [])]
print("tools/list:", code, names)
if EXA_TOOL not in names:
    sys.exit(f"{EXA_TOOL} not listed")

cases = [
    ("control: call Exa directly", EXA_TOOL, {"query": "Amazon Bedrock AgentCore", "objective": "Amazon Bedrock AgentCore", "numResults": 2}, "exa"),
    ("English -> AgentCore Web Search", "websearch___WebSearch", {"query": "Amazon Bedrock AgentCore Gateway interceptor", "maxResults": 2}, "websearch"),
    ("Chinese -> rewritten to Exa", "websearch___WebSearch", {"query": "杭州西湖 最佳游览季节", "maxResults": 2}, "exa"),
]
ok_all = True
for label, name, args, expect in cases:
    code, resp, text = call(name, args)
    err = resp.get("error") or (resp.get("result") or {}).get("isError")
    got = "websearch" if looks_like_websearch(text) else "exa"
    ok = got == expect and not err
    ok_all &= ok
    print(f"\n[{'PASS' if ok else 'FAIL'}] {label}\n  http={code} expect={expect} got={got} error={err}")
    print("  " + text[:500].replace("\n", " "))
sys.exit(0 if ok_all else 1)
