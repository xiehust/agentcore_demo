"""Gateway interceptor (REQUEST + RESPONSE) for language-based search routing.

REQUEST:
  - tools/call `websearch___WebSearch` with a mostly-CJK query is rewritten to DST_TOOL
    (mock Exa, real Exa or AnySearch; arguments are mapped per provider).
  - tools/call on any other tool is rejected (only EXPOSED_TOOL is public).
RESPONSE:
  - tools/list result is filtered down to EXPOSED_TOOL.
  - everything else passes through unchanged.
"""

import json
import os
import re

from normalize import normalize_text

EXPOSED_TOOL = "websearch___WebSearch"
DST_TOOL = os.environ.get("DST_TOOL", "mockexa___web_search_exa")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CJK_RATIO = 0.3


def is_chinese(text):
    chars = [c for c in text or "" if not c.isspace() and not c.isdigit()]
    if not chars:
        return False
    return sum(1 for c in chars if CJK.match(c)) / len(chars) >= CJK_RATIO


def on_request(body):
    if body.get("method") != "tools/call":
        return {"transformedGatewayRequest": {"body": body}}

    params = body.get("params") or {}
    name = params.get("name")
    if name != EXPOSED_TOOL:
        # Hidden tools are not callable directly; short-circuit with a JSON-RPC error.
        print(json.dumps({"blocked": name}))
        return {"transformedGatewayResponse": {"statusCode": 200, "body": {
            "jsonrpc": "2.0", "id": body.get("id"),
            "error": {"code": -32602, "message": f"Unknown tool: {name}"},
        }}}

    args = params.get("arguments") or {}
    if is_chinese(args.get("query", "")):
        new_args = map_args(DST_TOOL, args)
        body = {**body, "params": {**params, "name": DST_TOOL, "arguments": new_args}}
        print(json.dumps({"rerouted_to": DST_TOOL, "out": body}, ensure_ascii=False))
    return {"transformedGatewayRequest": {"body": body}}


def map_args(dst, args):
    """Translate WebSearch arguments into the destination tool's schema."""
    if dst.startswith("anysearch___"):
        out = {"query": args["query"]}
        if "maxResults" in args:
            out["max_results"] = min(int(args["maxResults"]), 10)  # AnySearch caps at 10
        return out
    out = {"query": args["query"], "objective": args["query"]}  # Exa requires objective
    if "maxResults" in args:
        out["numResults"] = args["maxResults"]
    return out


def normalize_tool_result(body):
    """Rewrite a successful search tools/call result into the unified schema."""
    result = body.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return body
    content = result.get("content") or []
    if len(content) != 1 or content[0].get("type") != "text":
        return body
    provider, text = normalize_text(content[0].get("text", ""))
    if provider is None:
        return body
    print(json.dumps({"normalized_from": provider}))
    # Drop provider-specific extras such as Exa's _meta so both paths look identical.
    return {**body, "result": {"content": [{"type": "text", "text": text}], "isError": False}}


def on_response(request_body, response):
    body = response.get("body") or {}
    method = (request_body or {}).get("method")
    if method == "tools/list" and isinstance(body.get("result"), dict):
        tools = body["result"].get("tools") or []
        kept = [t for t in tools if t.get("name") == EXPOSED_TOOL]
        print(json.dumps({"tools_list_filtered": [t.get("name") for t in tools], "kept": [t["name"] for t in kept]}))
        body = {**body, "result": {**body["result"], "tools": kept}}
        body["result"].pop("nextCursor", None)
    elif method == "tools/call":
        # Log which tool name the RESPONSE stage sees (original vs rewritten).
        print(json.dumps({"response_stage_tool": (request_body.get("params") or {}).get("name")}))
        body = normalize_tool_result(body)
    out = {"statusCode": response.get("statusCode", 200), "body": body}
    if response.get("headers"):
        out["headers"] = response["headers"]
    return {"transformedGatewayResponse": out}


def handler(event, context):
    mcp = event["mcp"]
    request_body = (mcp.get("gatewayRequest") or {}).get("body") or {}
    if mcp.get("gatewayResponse") is not None:
        result = on_response(request_body, mcp["gatewayResponse"])
    else:
        result = on_request(request_body)
    return {"interceptorOutputVersion": "1.0", "mcp": result}
