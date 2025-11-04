#!/usr/bin/env python3
"""
MCP SDK implementation of the Quip MCP server using mcp.server.fastmcp
This uses the official Model Context Protocol Python SDK
"""

import os
import sys
import urllib.parse
import boto3
from mcp.server.fastmcp import Context, FastMCP
from mcp_server_quip import get_document_content, init_quip_client
# Global variables
QUIP_ACCESS_TOKEN = None
QUIP_ACCESS_TOKEN_ARN = '/mcp/quip/apikey'

# Initialize FastMCP server from the official MCP SDK
mcp = FastMCP("quip-mcp-server",stateless_http=True)


def get_ssm_parameter(name: str, with_decryption: bool = True) -> str:
    """Get a parameter value from AWS Systems Manager Parameter Store."""
    ssm = boto3.client("ssm")
    response = ssm.get_parameter(Name=name, WithDecryption=with_decryption)
    return response["Parameter"]["Value"]


def ensure_access_token():
    """Ensure the Quip access token is available."""
    global QUIP_ACCESS_TOKEN
    if not QUIP_ACCESS_TOKEN:
        # Try environment variable first
        if 'QUIP_ACCESS_TOKEN' in os.environ:
            QUIP_ACCESS_TOKEN = os.environ['QUIP_ACCESS_TOKEN']
            print("Using access token from environment variable")
        else:
            # Fall back to SSM
            try:
                QUIP_ACCESS_TOKEN = get_ssm_parameter(QUIP_ACCESS_TOKEN_ARN)
                print(f"Retrieved access token from SSM: {QUIP_ACCESS_TOKEN_ARN}")
                os.environ['QUIP_ACCESS_TOKEN'] = QUIP_ACCESS_TOKEN
            except Exception as e:
                print(f"Warning: Could not retrieve token from SSM: {e}")
    return QUIP_ACCESS_TOKEN


@mcp.tool()
async def get_thread_metadata(
    thread_id: str
) -> str:
    """
    Get metadata of a Quip thread including its id, type, title and link.

    Args:
        thread_id: An unique id of a thread in Quip

    Returns:
        JSON string containing thread metadata
    """
    try:

        # Ensure access token is available
        token = ensure_access_token()
        if not token:
            error_msg = "Error: No Quip access token configured. Please set QUIP_ACCESS_TOKEN environment variable."
            print(error_msg)
            return error_msg

        # Get thread metadata from Quip
        client = init_quip_client(token)
        thread = client.get_thread(thread_id)["thread"]

        meta_data = {
            "thread_id": thread["id"],
            "thread_type": thread["type"],
            "thread_title": thread["title"],
            "thread_link": thread["link"]
        }

        return str(meta_data)

    except Exception as e:
        error_msg = f"Error fetching thread metadata: {str(e)}"
        print(error_msg)
        return error_msg


@mcp.tool()
async def get_thread_content(
    thread_id: str
) -> str:
    """
    Get the full content of a Quip thread.

    Args:
        thread_id: An unique id of a thread in Quip

    Returns:
        The content of the thread as text
    """
    try:
        # Ensure access token is available
        token = ensure_access_token()
        if not token:
            error_msg = "Error: No Quip access token configured. Please set QUIP_ACCESS_TOKEN environment variable."
            print(error_msg)
            return error_msg

        # Get thread content from Quip
        result = get_document_content(thread_id, token)

        return result

    except Exception as e:
        error_msg = f"Error fetching thread content: {str(e)}"
        print(error_msg)
        return error_msg



def main():
    """Start the MCP server"""
    print("=" * 70)
    print("Starting Quip MCP Server (Official MCP SDK Implementation)")
    print("=" * 70)
    print(f"Server name: quip-mcp-server")
    print(f"Framework: mcp.server.fastmcp (Official MCP Python SDK)")
    print(f"Transport: Streamable HTTP")
    print(f"Server will be available at: http://0.0.0.0:8000")
    print(f"MCP endpoint: http://0.0.0.0:8000/mcp")
    print("=" * 70)
    print("\nAvailable Tools:")
    print("  1. get_thread_metadata - Get Quip thread metadata")
    print("  2. get_thread_content  - Get Quip thread content")
    print("=" * 70)
    print("\nConfiguration:")
    print("  Set QUIP_ACCESS_TOKEN environment variable for authentication")
    print("  Optional: Set QUIP_BASE_URL for custom Quip instance")
    print("=" * 70)
    print("\nPress Ctrl+C to stop the server\n")

    try:
        # Run the MCP server with streamable-http transport
        # This is the official way to run an MCP server over HTTP
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        print("\n\nServer stopped by user")
    except Exception as e:
        print(f"\nError starting server: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
