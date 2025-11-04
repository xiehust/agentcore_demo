 #Create a sample MCP Server and host it in Runtime

from bedrock_agentcore_starter_toolkit import Runtime
from boto3.session import Session
import os
from dotenv import load_dotenv
load_dotenv("../.env")
from bedrock_agentcore.services.identity import IdentityClient

from boto3.session import Session
import boto3
boto_session = Session()
region = boto_session.region_name
assert os.environ.get("QUIP_API_KEY") != None

QUIP_ACCESS_TOKEN = '/mcp/quip/apikey'

def put_ssm_parameter(
    name: str,
    value: str,
    parameter_type: str = "String",
    with_encryption: bool = False
) -> None:
    """Put a parameter value into AWS Systems Manager Parameter Store."""
    ssm = boto3.client("ssm")
    put_params = {
        "Name": name,
        "Value": value,
        "Type": parameter_type,
        "Overwrite": True,
    }
    if with_encryption:
        put_params["Type"] = "SecureString"

    ssm.put_parameter(**put_params)
    
put_ssm_parameter(QUIP_ACCESS_TOKEN, os.environ.get("QUIP_API_KEY"))

boto_session = Session()
region = boto_session.region_name
print(f"Using AWS region: {region}")

required_files = ['start_remote_mcp_fastmcp.py', 'requirements.txt']
for file in required_files:
    if not os.path.exists(file):
        raise FileNotFoundError(f"Required file {file} not found")
print("All required files found ✓")
agentcore_runtime = Runtime()

runtime_client_id = os.environ.get("runtime_client_id")
runtime_cognito_discovery_url = os.environ.get("runtime_cognito_discovery_url")
auth_config = {
    "customJWTAuthorizer": {
        "allowedClients": [
            runtime_client_id
        ],
        "discoveryUrl": runtime_cognito_discovery_url
    }
}

print("Configuring AgentCore Runtime...")
response = agentcore_runtime.configure(
    entrypoint="start_remote_mcp_fastmcp.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name="ac_quip_mcp_server"
)
print("Configuration completed ✓")

print("Launching MCP server to AgentCore Runtime...")
print("This may take several minutes...")
launch_result = agentcore_runtime.launch(auto_update_on_conflict=True)

agent_arn = launch_result.agent_arn
agent_id = launch_result.agent_id

encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')

agent_url = f'https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT'
print("Launch completed ✓")
print(f"Agent ARN: {agent_arn}")
print(f"Agent ID: {agent_id}")
print(f"Agent agent_url: {agent_url}")


#Add extra required policies to auto-created role
## Since we are adding some outbound identity to our agent,
# we will need to get some API Keys and Secrets that are not available in the auto-created role. To do so, we will need to add some extra permissions to our auto-created IAM role. Let's first get this role and then add those permissions to it.

import json
agentcore_control_client = boto3.client(
    'bedrock-agentcore-control',
    region_name=region
)

runtime_response = agentcore_control_client.get_agent_runtime(
    agentRuntimeId=launch_result.agent_id
)
runtime_role = runtime_response['roleArn']

policies_to_add = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "GetResourceAPIKey",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetResourceApiKey"
            ],
            "Resource": "*"
        },
        {
            "Sid": "SecretManager",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:*:*:secret:bedrock-agentcore*"
        },
        {
            "Sid": "SSMGet",
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameter"
            ],
            "Resource": "arn:aws:ssm:*:*:parameter/mcp/quip/apikey"
        }
    ]
}
iam_client = boto3.client(
    'iam',
    region_name=region
)

response = iam_client.put_role_policy(
    PolicyDocument=json.dumps(policies_to_add),
    PolicyName="outbound_policies",
    RoleName=runtime_role.split("/")[1],
)
print(response)
print(f"Added policies to auto-created role:{runtime_role} ✓")