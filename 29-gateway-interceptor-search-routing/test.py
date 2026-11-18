"""Call the gateway over MCP (SigV4) and check where each tools/call actually landed."""

import json
import sys
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
    text = r.text
    if text.startswith("event:") or "data:" in text[:50]:
        text = next(l[5:].strip() for l in text.splitlines() if l.startswith("data:"))
    return r.status_code, json.loads(text)


def call(name, args):
    code, resp = rpc("tools/call", {"name": name, "arguments": args})
    content = (resp.get("result") or {}).get("content") or []
    text = content[0].get("text", "") if content else json.dumps(resp, ensure_ascii=False)
    return code, resp, text


def landed(text):
    return "mockexa" if '"routed_to": "mockexa"' in text or "'routed_to': 'mockexa'" in text or '"routed_to":"mockexa"' in text else "websearch"


results = []

if __name__ == "__main__":
    code, tl = rpc("tools/list")
    names = [t["name"] for t in tl.get("result", {}).get("tools", [])]
    print("tools/list:", code, names)

    cases = [
        ("control: call mockexa directly", "mockexa___web_search_exa", {"query": "hello"}, "mockexa"),
        ("English -> websearch (no rewrite)", "websearch___WebSearch", {"query": "AWS re:Invent 2025 announcements", "maxResults": 2}, "websearch"),
        ("Chinese -> rewritten to mockexa", "websearch___WebSearch", {"query": "杭州今天的天气怎么样", "maxResults": 2}, "mockexa"),
        ("Mixed (>=30% CJK) -> mockexa", "websearch___WebSearch", {"query": "AgentCore 网关拦截器怎么配置", "maxResults": 2}, "mockexa"),
    ]

    for label, name, args, expect in cases:
        code, resp, text = call(name, args)
        err = resp.get("error") or (resp.get("result") or {}).get("isError")
        got = landed(text)
        ok = (got == expect) and not err
        results.append(ok)
        print(f"\n[{'PASS' if ok else 'FAIL'}] {label}\n  http={code} expect={expect} got={got} error={err}")
        print("  " + text[:400].replace("\n", " "))

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
