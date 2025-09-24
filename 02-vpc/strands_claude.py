from bedrock_agentcore.runtime import BedrockAgentCoreApp
app = BedrockAgentCoreApp()

from strands import Agent, tool
from strands.models import BedrockModel
from strands_tools import calculator, file_read, shell, current_time
import json
import os
import asyncio
import argparse
from strands.tools.mcp import MCPClient
from mcp import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

@tool
def query_order(order_id:str):
    """query the order status by given order id"""
    return "Shipped"

# MCP Client Setup

# mcp_server MCP Client
mcp_server_client_0266 = MCPClient(
    lambda: streamablehttp_client("https://knowledge-mcp.global.api.aws"),
    startup_timeout=30
)


# Agent Configuration
agent_model = BedrockModel(
    model_id="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    temperature=0.7,
    max_tokens=4000
)

# Main execution
async def main(user_input_arg: str = None, messages_arg: str = None):
    global mcp_server_client_0266, query_order

    # Use MCP clients in context managers (only those connected to execution agent)
    with mcp_server_client_0266:
        # Get tools from MCP servers
        mcp_tools = []
        mcp_tools.extend(mcp_server_client_0266.list_tools_sync())
        
        # Create agent with MCP tools
        agent = Agent(
            model=agent_model,
            system_prompt="""You are a helpful AI assistant.""",
            tools=mcp_tools + [query_order],
            callback_handler=None
        )
        # User input from command-line arguments with priority: --messages > --user-input > default
        if messages_arg is not None and messages_arg.strip():
            # Parse messages JSON and pass full conversation history to agent
            try:
                messages_list = json.loads(messages_arg)
                # Pass the full messages list to the agent
                user_input = messages_list
            except (json.JSONDecodeError, KeyError, TypeError):
                user_input = "Hello, how can you help me?"
        elif user_input_arg is not None and user_input_arg.strip():
            user_input = user_input_arg.strip()
        else:
            # Default fallback when no input provided
            user_input = "Hello, how can you help me?"
        # Execute agent with streaming
        async for event in agent.stream_async(user_input):
            if "data" in event:
                print(event['data'],end='',flush=True)
                yield event['data']

@app.entrypoint
async def entry(payload):
    user_input_param = payload.get('user_input')
    messages_param = payload.get('messages')
    async for event in main(user_input_param, messages_param):
        yield event

if __name__ == "__main__":
    app.run()