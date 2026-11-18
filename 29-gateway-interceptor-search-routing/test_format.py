"""Both search paths return the same unified JSON schema through the single exposed tool."""

import json
import sys
from pathlib import Path

from test import call

EXPOSED = "websearch___WebSearch"
# Chinese-route provider chosen by `deploy.py --cn-provider` (mock has no unified format, so not tested here)
CN = json.loads((Path(__file__).parent / "state.json").read_text()).get("cn_provider", "exa")
KEYS = {"title", "url", "publishedDate", "author", "text"}
results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}  {detail}")


for q, provider in [("Amazon Bedrock AgentCore Gateway interceptor", "agentcore-web-search"),
                    ("杭州西湖 最佳游览季节", CN)]:
    code, resp, text = call(EXPOSED, {"query": q, "maxResults": 2})
    res = resp.get("result") or {}
    try:
        data = json.loads(text)
    except ValueError:
        check(f"{provider}: result is JSON", False, text[:120])
        continue
    items = data.get("results") or []
    check(f"{provider}: provider field", data.get("provider") == provider, f"got={data.get('provider')}")
    check(f"{provider}: >=1 result, every item has exactly {sorted(KEYS)}",
          bool(items) and all(set(i) == KEYS for i in items), f"n={len(items)}")
    check(f"{provider}: result envelope identical (content+isError only)",
          set(res) == {"content", "isError"}, f"keys={sorted(res)}")
    for i in items:
        print(f"    - {i['publishedDate']} | {i['title'][:50]} | {i['url'][:70]} | text {len(i['text'])} chars")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
