"""
Run from the repository root:
    uv run examples/snippets/clients/streamable_basic.py
"""

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

token="eyJraWQiOiJQYmgxWk1XeUJRWTJLSURrVEhCNXQ4RkE2Q1pWQzN4NVJ6WWNUYzFOcW84PSIsImFsZyI6IlJTMjU2In0.eyJzdWIiOiIybmRjaXVmbzMydDA3cDlhNDUwN2wzbGtlbiIsInRva2VuX3VzZSI6ImFjY2VzcyIsInNjb3BlIjoibXktYXBpXC93cml0ZSBteS1hcGlcL3JlYWQiLCJhdXRoX3RpbWUiOjE3NjMzNjc2MjQsImlzcyI6Imh0dHBzOlwvXC9jb2duaXRvLWlkcC51cy1lYXN0LTEuYW1hem9uYXdzLmNvbVwvdXMtZWFzdC0xX01UVnJ6YkJNWCIsImV4cCI6MTc2MzM3MTIyNCwiaWF0IjoxNzYzMzY3NjI0LCJ2ZXJzaW9uIjoyLCJqdGkiOiJhZjkzNmNjMS00ZjUzLTQzOTAtOTkyMC02MWM2YzlhNzkxMzkiLCJjbGllbnRfaWQiOiIybmRjaXVmbzMydDA3cDlhNDUwN2wzbGtlbiJ9.F3i-sg4X9Q4Lqp1iM4O8tYbpFbwlNsei_55WKV7Rf6Z-M9nLZz0sLQ3wFdkhQfJzwTOXpdSbNV1eCe5Fi0mIyhRn-0vXzOdUIJNpxriAz1NBySgFVTF61zoWC9rXh_C2_amDAs7dr_ESDXi46gCaxmskWwLBscssqwYyGo2p5l6egCj_VfHY9A-x3ogzI4VBVP4s5OsT8sc8xwxaHTIvL5sTGfn3xNQoNzuTC0Uh_5-9ma3H7PT-zsEKZKIygBBsPD7aSSkUzfPeXHiYsf1OZAY51QW1lXsGMl13NbL15yxugjGoXZ0fHgvLvLdfYF5TZcguCehDXAc7Z8URXr4K3w"
url="https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A434444145045%3Aruntime%2Ffeishu_mcp-vZoq8mDAuS/invocations?qualifier=DEFAULT"
async def main():
    # Connect to a streamable HTTP server
    async with streamablehttp_client(
        # "http://127.0.0.1:8000/mcp"
         url, headers={"Authorization": f"Bearer {token}"}        
                                     ) as (
        read_stream,
        write_stream,
        _,
    ):
        # Create a session using the client streams
        async with ClientSession(read_stream, write_stream) as session:
            # Initialize the connection
            await session.initialize()
            # List available tools
            tools = await session.list_tools()
            print(f"Available tools: {[tool.name for tool in tools.tools]}")


if __name__ == "__main__":
    asyncio.run(main())