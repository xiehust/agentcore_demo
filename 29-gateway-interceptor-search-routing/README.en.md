# Gateway interceptor: route web search by query language

[中文](README.md)

An AgentCore Gateway interceptor (one Lambda at REQUEST + RESPONSE) routes a single
search tool by query language:

- English queries go to **AgentCore Web Search** (`websearch___WebSearch`).
- Chinese queries (>= 30% CJK characters) are rewritten to a third-party MCP search target:
  **AnySearch** (default), **Bocha** or **Exa**, selected with `deploy.py --cn-provider`.

The agent sees only `websearch___WebSearch`; the third-party tools are hidden from
`tools/list` and rejected on direct call. All providers return one unified result format.

Gateway inbound auth is AWS_IAM (SigV4). Region us-east-1.

## Run

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt   # pinned: boto3/botocore 1.44.5, requests 2.34.2

# store the key(s) you need in AgentCore Identity (read from stdin, never written to disk)
printf %s "<anysearch key>" | .venv/bin/python store_anysearch_key.py
printf %s "<bocha key>"     | .venv/bin/python store_bocha_key.py
printf %s "<exa key>"       | .venv/bin/python store_exa_key.py

.venv/bin/python deploy.py --cn-provider anysearch   # or bocha / exa; sets the interceptor's DST_TOOL
.venv/bin/python test_exposure.py                    # 10 cases
.venv/bin/python test_format.py                      # 6 cases
.venv/bin/python cleanup.py                          # deletes everything, incl. harness and API key providers
```

`deploy.py` creates a target only for providers whose key is stored. Tests read the
current route (`cn_provider`) from `state.json`. `test_exa.py` is an early test that
calls Exa directly; with tool hiding on, its control case fails by design.

## Targets and credentials

| target | endpoint | header injected by the Gateway |
|---|---|---|
| `websearch` | AgentCore Web Search connector | Gateway IAM role |
| `anysearch` | `https://api.anysearch.com/mcp` | `Authorization: Bearer <key>` (`credentialPrefix="Bearer "`) |
| `bocha` | `https://mcp.bochaai.com/mcp` | `Authorization: Bearer <key>` |
| `exa` | `https://mcp.exa.ai/mcp` | `x-api-key: <key>` |

AnySearch's sample `X-Anysearch-Client` header is not required. For AnySearch and Bocha an
invalid key returns `isError: true`, so successful searches through the Gateway confirm the
stored key is used.

## Argument mapping

| WebSearch | AnySearch `search` | Bocha `bocha_web_search` | Exa `web_search_exa` |
|---|---|---|---|
| `query` | `query` | `query` | `query` |
| `maxResults` | `max_results` (capped at 10) | `count` | `numResults` |
| - | - | - (`freshness` defaults to `noLimit`) | `objective` (required, filled with query) |

## Unified result format

The RESPONSE stage rewrites each successful `tools/call` result (`lambdas/normalize.py`) into:

```json
{"provider": "agentcore-web-search | anysearch | bocha | exa",
 "results": [{"title": "", "url": "", "publishedDate": "YYYY-MM-DD | null", "author": "str | null", "text": ""}]}
```

The envelope is reduced to `content` + `isError`. The RESPONSE stage sees the original
request (`websearch___WebSearch`) even after rerouting, so the provider is detected from the payload:

| provider | raw format | detected by |
|---|---|---|
| AgentCore Web Search | JSON | JSON with `results` |
| AnySearch | Markdown (`### N. Title`, `- **URL**:`, trailing ` date: Aug 14, 2026`) | starts with `## Search Results` |
| Bocha | text: `Title/URL/Description/Published date/Site name`, blank-line separated | starts with `Title:` and has a Bocha-only field |
| Exa | text: `Title/URL/Published/Author/Highlights`, `---` separated | starts with `Title:`, no Bocha fields |

## Findings (2026-11-18)

- The Gateway routes on the tool name rewritten by the REQUEST interceptor (not documented; verified by test).
- RESPONSE still runs after a REQUEST short-circuit (documented).
- Third-party targets bring extra tools (Exa `agent_run`/`web_fetch_exa`; AnySearch `batch_search`/`extract`/`get_sub_domains`/`score`; Bocha `bocha_ai_search`); all hidden and rejected.

## End-to-end: AgentCore Managed Harness

`harness_demo.py` creates a managed agent and searches through this Gateway:

- Model `us.openai.gpt-6.1-terra` (Bedrock inference profile).
- Tool type `agentcore_gateway` with outbound auth `awsIam`, matching the Gateway's `AWS_IAM` inbound auth.
- Execution role `ic-reroute-poc-harness-role`: invoke this model, invoke this Gateway, write `/aws/bedrock-agentcore/*` logs.
- `memory={"disabled": {}}`: otherwise the Harness creates a managed memory and the role also needs
  access to it (e.g. `bedrock-agentcore:ListEvents`), or the first invocation fails.

```bash
.venv/bin/python harness_demo.py              # one Chinese and one English question
.venv/bin/python harness_demo.py "<question>" # custom question
```

Result (2026-11-18, Chinese route = AnySearch): the model called `websearch___WebSearch` both times.
"杭州西湖一年中什么时候去最好？" was routed to AnySearch (ly.com, trip.com, zhihu results);
"What are AgentCore Gateway interceptors used for?" stayed on AgentCore Web Search (AWS docs).
The prompt never mentions AnySearch.

## Limitations

- Text parsing depends on each provider's current format; unknown formats pass through unchanged.
  Bocha and Exa both start with `Title:` and are told apart by field names.
- AnySearch relative dates ("2 days ago") become `null`; AnySearch's requested `score` feedback is not sent.
- Chinese queries leave AWS for a third-party provider; check compliance requirements.
- The Harness's built-in `web_fetch` tool does not go through the Gateway. Restricting tools with `allowedTools`
  is unresolved: `websearch___WebSearch`, `search` and `search___*` all removed the Gateway tool as well.
