import boto3
import utils
import os
from dotenv import load_dotenv
load_dotenv()
REGION = 'us-west-2'
gw_user_pool_id = os.environ.get("gw_user_pool_id")
gw_client_id= os.environ.get("gw_client_id")
gw_client_secret= os.environ.get("gw_client_secret")
scopeString = "sample-agentcore-gateway-id/invoke"
gatewayURL = os.environ.get("gatewayURL")
token_response = utils.get_token(gw_user_pool_id, gw_client_id, gw_client_secret, scopeString, REGION)
print(token_response)
token = token_response["access_token"]
print("Token response:", token)

#Search for MCP Server tools through the Gateway

import requests
import json

def search_tools(gateway_url, access_token, query):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    payload = {
        "jsonrpc": "2.0",
        "id": "search-tools-request",
        "method": "tools/call",
        "params": {
            "name": "x_amz_bedrock_agentcore_search",
            "arguments": {
                "query": query
            }
        }
    }

    response = requests.post(gateway_url, headers=headers, json=payload)
    return response.json()


# Example usage
token_response = utils.get_token(gw_user_pool_id, gw_client_id, gw_client_secret, scopeString, REGION)
access_token = token_response['access_token']
results = search_tools(gatewayURL, access_token, "orders")
print(json.dumps(results, indent=2))


#Ask Agent from MCP tool to operate on orders

import json

import requests
from strands.models import BedrockModel
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient
from strands import Agent


def get_token():
    token = utils.get_token(gw_user_pool_id, gw_client_id, gw_client_secret, scopeString, REGION)
    return token['access_token']


def create_streamable_http_transport():
    return streamablehttp_client(
        gatewayURL, headers={"Authorization": f"Bearer {get_token()}"}
    )


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
    # Simplified prompt and error handling
    result = agent("总结这个文档https://quip-amazon.com/")
