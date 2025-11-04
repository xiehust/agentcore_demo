import os
import logging
from mcp import stdio_client, StdioServerParameters
from strands import Agent
from strands.multiagent.a2a import A2AServer
from strands.tools.mcp import MCPClient
from fastapi import FastAPI
import uvicorn


logging.basicConfig(level=logging.INFO)
app = FastAPI()

# Use the complete runtime URL from environment variable, fallback to local
runtime_url = os.environ.get('AGENTCORE_RUNTIME_URL', 'http://127.0.0.1:9000/')
host, port = "0.0.0.0", 9000



stdio_mcp_client = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(
            command="uvx", args=["awslabs.aws-documentation-mcp-server@latest"]
        )
    )
)

stdio_mcp_client.start()

system_prompt = """You are an AWS Documentation Assistant powered by the AWS Documentation MCP server. Your role is to help users find accurate, up-to-date information from AWS documentation.

Key capabilities:
- Search and retrieve information from AWS service documentation
- Provide clear, accurate answers about AWS services, features, and best practices
- Help users understand AWS concepts, APIs, and configuration options
- Guide users to relevant AWS documentation sections

Guidelines:
- Always prioritize official AWS documentation as your source of truth
- Provide specific, actionable information when possible
- Include relevant links or references to AWS documentation when helpful
- If you're unsure about something, clearly state your limitations
- Focus on being helpful, accurate, and concise in your responses
- Try to simplify/summarize answers to make it faster, small and objective

You have access to AWS documentation search tools to help answer user questions effectively."""

agent = Agent(system_prompt=system_prompt, 
              tools=[stdio_mcp_client.list_tools_sync()],
              name="AWS Docs Agent",
              description="An agent to query AWS Docs using AWS MCP.",
              callback_handler=None)

# Pass runtime_url to http_url parameter AND use serve_at_root=True
a2a_server = A2AServer(
    agent=agent,
    http_url=runtime_url,
    serve_at_root=True  # Serves locally at root (/) regardless of remote URL path complexity
)

@app.get("/ping")
def ping():
    return {"status": "healthy"}

app.mount("/", a2a_server.to_fastapi_app())

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)