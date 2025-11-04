from helpers.utils import setup_cognito_user_pool, reauthenticate_user
import time 
# Import libraries
import os
import json
import requests
import boto3
import time
from boto3.session import Session
from strands.tools import tool

# Get boto session
boto_session = Session()
region = boto_session.region_name

print(f"Region: {region}")
print("Setting up Amazon Cognito user pool...")
cognito_config = (
    setup_cognito_user_pool()
)  # You'll get your bearer token from this output cell.
print("Cognito setup completed ✓")

from helpers.utils import create_agentcore_runtime_execution_role, AWS_DOCS_ROLE_NAME

execution_role_arn_mcp = create_agentcore_runtime_execution_role(AWS_DOCS_ROLE_NAME)
print("Execution role ARN:", execution_role_arn_mcp)

from helpers.utils import create_agentcore_runtime_execution_role, AWS_BLOG_ROLE_NAME

execution_role_arn_blogs = create_agentcore_runtime_execution_role(AWS_BLOG_ROLE_NAME)
print("Execution role ARN:", execution_role_arn_blogs)


from bedrock_agentcore_starter_toolkit import Runtime

agentcore_runtime_mcp_agent = Runtime()
aws_docs_agent_name="aws_docs_assistant"

# Configure the deployment
response_aws_docs_agent = agentcore_runtime_mcp_agent.configure(
    entrypoint="agents/strands_aws_docs.py",
    execution_role=execution_role_arn_mcp,
    auto_create_ecr=True,
    requirements_file="agents/requirements.txt",
    region=region,
    agent_name=aws_docs_agent_name,
    authorizer_configuration={
        "customJWTAuthorizer": {
            "allowedClients": [cognito_config.get("client_id")],
            "discoveryUrl": cognito_config.get("discovery_url"),
        }
    },
    protocol="A2A",
)

print("Configuration completed:", response_aws_docs_agent)



launch_result_mcp = agentcore_runtime_mcp_agent.launch()
print("Launch completed:", launch_result_mcp.agent_arn)

docs_agent_arn = launch_result_mcp.agent_arn

# Wait for the agent to be ready
status_response = agentcore_runtime_mcp_agent.status()
status = status_response.endpoint["status"]

end_status = ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]
while status not in end_status:
    print(f"Waiting for deployment... Current status: {status}")
    time.sleep(10)
    status_response = agentcore_runtime_mcp_agent.status()
    status = status_response.endpoint["status"]

print(f"Final status: {status}")


from bedrock_agentcore_starter_toolkit import Runtime

agentcore_runtime_blogs = Runtime()
aws_blogs_agent_name="aws_blog_assistant"

# Configure the deployment
response_aws_blogs_agent = agentcore_runtime_blogs.configure(
    entrypoint="agents/strands_aws_blogs_news.py",
    execution_role=execution_role_arn_blogs,
    auto_create_ecr=True,
    requirements_file="agents/requirements.txt",
    region=region,
    agent_name=aws_blogs_agent_name,
    authorizer_configuration={
        "customJWTAuthorizer": {
            "allowedClients": [cognito_config.get("client_id")],
            "discoveryUrl": cognito_config.get("discovery_url"),
        }
    },
    protocol="A2A"
)

print("Configuration completed:", response_aws_blogs_agent)

launch_result_blog = agentcore_runtime_blogs.launch()
print("Launch completed:", launch_result_blog.agent_arn)

blog_agent_arn = launch_result_blog.agent_arn

# Wait for the agent to be ready
status_response = agentcore_runtime_blogs.status()
status = status_response.endpoint["status"]

end_status = ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]
while status not in end_status:
    print(f"Waiting for deployment... Current status: {status}")
    time.sleep(10)
    status_response = agentcore_runtime_blogs.status()
    status = status_response.endpoint["status"]

print(f"Final status: {status}")

MCP_AGENT_ID = launch_result_mcp.agent_id
MCP_AGENT_ARN = launch_result_mcp.agent_arn
MCP_AGENT_NAME = aws_docs_agent_name

BLOG_AGENT_ID = launch_result_blog.agent_id
BLOG_AGENT_ARN = launch_result_blog.agent_arn
BLOG_AGENT_NAME = aws_blogs_agent_name

COGNITO_CLIENT_ID = cognito_config.get("client_id")
COGNITO_SECRET = cognito_config.get("client_secret")
DISCOVERY_URL = cognito_config.get("discovery_url")

print(f"Agent ID: {MCP_AGENT_ID}")
print(f"Agent ARN: {MCP_AGENT_ARN}")
print(f"Agent Name: {MCP_AGENT_NAME}")
print(f"Agent ID: {BLOG_AGENT_ID}")
print(f"Agent ARN: {BLOG_AGENT_ARN}")
print(f"Agent Name: {BLOG_AGENT_NAME}")
print(f"Cognito Client ID: {COGNITO_CLIENT_ID}")
print(f"Cognito Secret: {COGNITO_SECRET}")
print(f"Discovery URL: {DISCOVERY_URL}")


from helpers.utils import put_ssm_parameter, SSM_DOCS_AGENT_ARN, SSM_BLOGS_AGENT_ARN

put_ssm_parameter(SSM_DOCS_AGENT_ARN, MCP_AGENT_ARN)

put_ssm_parameter(SSM_BLOGS_AGENT_ARN, BLOG_AGENT_ARN)