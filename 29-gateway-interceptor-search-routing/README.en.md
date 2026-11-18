# Gateway REQUEST interceptor reroute PoC

[中文](README.md)

Checks whether an AgentCore Gateway REQUEST interceptor can reroute a `tools/call`
to a different target by rewriting `params.name`.

- `websearch` target: AgentCore Web Search connector (`websearch___WebSearch`)
- `mockexa` target: Lambda standing in for Exa (`mockexa___web_search_exa`)
- `lambdas/interceptor.py`: if the query is >= 30% CJK, rewrites the tool name to
  `mockexa___web_search_exa` and maps `maxResults` -> `numResults`

Gateway inbound auth is AWS_IAM (SigV4). Region us-east-1.

```bash
uv venv .venv && uv pip install --python .venv/bin/python boto3 requests
.venv/bin/python deploy.py    # writes state.json
.venv/bin/python test.py      # 4 cases
.venv/bin/python cleanup.py   # deletes targets, gateway, Lambdas, IAM roles
```

Result (2026-11-18): 4/4 passed. The Gateway routes on the rewritten tool name,
and the target receives the rewritten name and arguments.

## Real Exa

```bash
printf %s "$EXA_KEY" | .venv/bin/python store_exa_key.py   # key -> AgentCore Identity
.venv/bin/python deploy.py                                 # adds `exa` MCP target (x-api-key header)
# then: .venv/bin/python deploy.py --cn-provider exa   (sets DST_TOOL)
.venv/bin/python test_exa.py
```

Exa's `web_search_exa` requires `objective`, so the interceptor fills it with the query.
Result (2026-11-18): 3/3 passed; Chinese query rewritten to Exa returned Exa results.

## Single exposed tool (REQUEST + RESPONSE interceptor)

The same Lambda now runs at both interception points:
- RESPONSE: `tools/list` is filtered to `websearch___WebSearch` only.
- REQUEST: `tools/call` on any other tool is short-circuited with JSON-RPC error -32602.

```bash
.venv/bin/python deploy.py          # adds RESPONSE interception point via update_gateway
.venv/bin/python test_exposure.py   # 6 cases
```

Result (2026-11-18): 6/6 passed. `test.py` / `test_exa.py` call hidden tools directly,
so their control cases now fail by design.

## Unified result format

The RESPONSE stage rewrites every successful `tools/call` result (see `lambdas/normalize.py`) into:

```json
{"provider": "agentcore-web-search | anysearch | exa",
 "results": [{"title": "", "url": "", "publishedDate": "YYYY-MM-DD | null", "author": "str | null", "text": ""}]}
```

The result envelope is reduced to `content` + `isError` (Exa's `_meta` is dropped).
The provider is detected from the payload, because the RESPONSE stage sees the
original request (`websearch___WebSearch`) even when the REQUEST stage rerouted it.

```bash
.venv/bin/python test_format.py     # 6 cases
.venv/bin/python test_exposure.py   # still 6/6
```

## AnySearch (default Chinese route)

```bash
printf %s "<anysearch key>" | .venv/bin/python store_anysearch_key.py   # key -> AgentCore Identity
.venv/bin/python deploy.py --cn-provider anysearch                       # adds `anysearch` MCP target, sets DST_TOOL
.venv/bin/python test_exposure.py   # 9 cases
.venv/bin/python test_format.py     # 6 cases
```

- Target `https://api.anysearch.com/mcp`; the Gateway injects `Authorization: Bearer <key>`
  (`credentialParameterName=Authorization`, `credentialPrefix="Bearer "`). The
  `X-Anysearch-Client` header from AnySearch's sample config is not required.
- `maxResults` maps to `max_results` (capped at 10). Only general search is used, no `domain`/`sub_domain`.
- AnySearch returns Markdown (`### N. Title`, `- **URL**:`, trailing ` date: Aug 14, 2026`);
  `normalize.py` parses it into the unified format. Relative dates ("2 days ago") become `null`.
- Its extra tools (`batch_search`, `extract`, `get_sub_domains`, `score`) are hidden and rejected.
  AnySearch asks agents to submit `score` feedback; this PoC does not.
- `deploy.py --cn-provider mock|exa|anysearch` switches the Chinese route; tests read `cn_provider` from `state.json`.

Result (2026-11-18): test_exposure 9/9, test_format 6/6. An invalid key called directly
returns `isError: true`, so successful searches through the Gateway confirm the stored key is used.
