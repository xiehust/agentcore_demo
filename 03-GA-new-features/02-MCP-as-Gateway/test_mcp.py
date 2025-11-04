import boto3
import utils
import os
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from dotenv import load_dotenv
load_dotenv()
REGION = 'us-west-2'
runtime_user_pool_id = os.environ.get("runtime_user_pool_id")
runtime_client_id= os.environ.get("runtime_client_id")
runtime_client_secret= os.environ.get("runtime_client_secret")
scopeString = "sample-agentcore-runtime-id/invoke"
token_response = utils.get_token(runtime_user_pool_id, runtime_client_id, runtime_client_secret, scopeString, REGION)
token = token_response["access_token"]
print("Token response:", token)

import requests
from strands.models import BedrockModel
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient
from strands import Agent
print("==================")
mcpURL = os.environ.get("runtimeURL")
def create_streamable_http_transport():
    return streamablehttp_client(
        mcpURL, headers={"Authorization": f"Bearer {token}"}
    )


async def main():
    mcp_url = mcpURL
    headers ={"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(mcp_url, headers, timeout=30, terminate_on_close=False) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tool_result = await session.list_tools()
            print(tool_result)

asyncio.run(main())


client = MCPClient(create_streamable_http_transport)

## The IAM group/user/ configured in ~/.aws/credentials should have access to Bedrock model
yourmodel = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0", # may need to update model_id depending on region
    temperature=0.7,
    max_tokens=2000,  # Limit response length
)

with client:
    # Call the listTools
    tools = client.list_tools_sync()
    # Create an Agent with the model and tools
    agent = Agent(
        model=yourmodel, tools=tools
    )  ## you can replace with any model you like
    # Invoke the agent with the sample prompt. This will only invoke MCP listTools and retrieve the list of tools the LLM has access to. The below does not actually call any tool.
    # agent("Hi, can you list all tools available to you")
    agent("总结这个quip)