"""
Strands agent on AgentCore Runtime (networkMode=PUBLIC), backed by Nova 2 Lite.

End-to-end shape:

    caller -> AgentCore Runtime (PUBLIC, no VPC)      <- this file
                -> Bedrock  global.amazon.nova-2-lite-v1:0
                -> AgentCore Gateway (MCP, AWS_IAM)
                     -> Lambda (VPC-attached)  -> private RDS MySQL
                     -> API Gateway -> VPC Link -> NLB -> EC2 -> private RDS MySQL

The runtime itself has no VPC configuration at all, yet the agent reads data out
of a private database in an isolated VPC. That is the "sink the egress into a
tool" pattern (workaround 4 of the no-VPC-egress design doc) working in practice.

Payload: {"prompt": "...", "session_id": "optional"}
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

import httpx
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp.client.streamable_http import streamable_http_client
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

sys.path.insert(0, os.path.dirname(__file__))
from sigv4_auth import SigV4HttpxAuth  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nova2-agent")

MODEL_ID = os.environ.get("MODEL_ID", "global.amazon.nova-2-lite-v1:0")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "").strip()
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"

SYSTEM_PROMPT = """You are an order-desk assistant for an internal operations team.

The order data lives in a private company database that you can only reach through
your tools. Rules:
- Always call a tool to answer questions about orders or the database. Never invent
  order references, emails, amounts or statuses.
- Valid statuses are SHIPPED, PENDING and CANCELLED.
- After a tool returns, answer in one or two short sentences and include the concrete
  values you retrieved.
"""

app = BedrockAgentCoreApp()


def make_gateway_client() -> MCPClient:
    """MCP client for the AgentCore Gateway, authenticated with SigV4.

    `streamable_http_client` does not close an httpx client that was passed in,
    so this wrapper owns it — the `async with` runs on MCP's own event loop, which
    is the only place the connection pool can be torn down safely. Without this,
    every invocation would leak a pool.
    """
    auth = SigV4HttpxAuth(service="bedrock-agentcore", region=REGION)

    @asynccontextmanager
    async def transport():
        async with httpx.AsyncClient(
            auth=auth,
            timeout=httpx.Timeout(60.0, read=300.0),
            follow_redirects=True,
        ) as http_client:
            async with streamable_http_client(
                    GATEWAY_URL, http_client=http_client) as streams:
                yield streams

    return MCPClient(transport)


def summarise_tool_calls(messages) -> list[dict]:
    """Pull the tool invocations out of the conversation, as run evidence."""
    calls = []
    for message in messages:
        for block in message.get("content") or []:
            if isinstance(block, dict) and "toolUse" in block:
                use = block["toolUse"]
                calls.append({"name": use.get("name"), "input": use.get("input")})
    return calls


def run(prompt: str) -> dict:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)

    if not GATEWAY_URL:
        log.warning("GATEWAY_URL unset — running with no tools")
        agent = Agent(model=model, system_prompt=SYSTEM_PROMPT)
        result = agent(prompt)
        return {"result": str(result), "model": MODEL_ID, "tools_available": [],
                "tool_calls": summarise_tool_calls(agent.messages)}

    # The MCP session lives only for this invocation, so concurrent runtime
    # sessions never share one.
    with make_gateway_client() as gateway:
        tools = gateway.list_tools_sync()
        names = [getattr(t, "tool_name", str(t)) for t in tools]
        log.info("gateway exposed %d tools: %s", len(tools), names)

        agent = Agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)
        result = agent(prompt)

        return {
            "result": str(result),
            "model": MODEL_ID,
            "tools_available": names,
            "tool_calls": summarise_tool_calls(agent.messages),
        }


@app.entrypoint
def invoke(payload):
    prompt = (payload or {}).get("prompt") or "How many orders are pending?"
    log.info("prompt=%r", prompt)
    try:
        return run(prompt)
    except Exception as exc:
        log.exception("invocation failed")
        return {"error": f"{type(exc).__name__}: {exc}", "model": MODEL_ID}


if __name__ == "__main__":
    app.run()
