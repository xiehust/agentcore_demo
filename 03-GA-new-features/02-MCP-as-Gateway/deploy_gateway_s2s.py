import os
import sys


# Get the directory of the current script
if '__file__' in globals():
    current_dir = os.path.dirname(os.path.abspath(__file__))
else:
    current_dir = os.getcwd()  # Fallback if __file__ is not defined (e.g., Jupyter)

# Navigate to the directory containing utils.py (one level up)
utils_dir = os.path.abspath(os.path.join(current_dir, '..'))

# Add to sys.path
sys.path.insert(0, utils_dir)

# Now you can import utils
import utils

# Setup logging 
import logging

# Configure logging for notebook environment
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

# Set specific logger levels
# logging.getLogger("strands").setLevel(logging.INFO)


#Create an IAM role for the Gateway to assume
agentcore_gateway_iam_role = utils.create_agentcore_gateway_role("sample-mcpgateway")
print("Agentcore gateway role ARN: ", agentcore_gateway_iam_role['Role']['Arn'])


#Create Amazon Cognito Pool for Inbound authorization to Gateway
# Creating Cognito User Pool 
import os
import boto3
from boto3.session import Session
# Get boto session
boto_session = Session()
REGION = boto_session.region_name
USER_POOL_NAME = "sample-agentcore-gateway-pool"
RESOURCE_SERVER_ID = "sample-agentcore-gateway-id"
RESOURCE_SERVER_NAME = "sample-agentcore-gateway-name"
CLIENT_NAME = "sample-agentcore-gateway-client"
SCOPES = [
    {"ScopeName": "invoke",  # Just 'invoke', will be formatted as resource_server_id/invoke
    "ScopeDescription": "Scope for invoking the agentcore gateway"},
]

scope_names = [f"{RESOURCE_SERVER_ID}/{scope['ScopeName']}" for scope in SCOPES]
scopeString = " ".join(scope_names)
print(f"Gateway scopeString:{scopeString}")

cognito = boto3.client("cognito-idp", region_name=REGION)

print("Creating or retrieving Cognito resources...")
gw_user_pool_id = utils.get_or_create_user_pool(cognito, USER_POOL_NAME)
print(f"Gateway User Pool ID: {gw_user_pool_id}")

utils.get_or_create_resource_server(cognito, gw_user_pool_id, RESOURCE_SERVER_ID, RESOURCE_SERVER_NAME, SCOPES)
print("Resource server ensured.")

gw_client_id, gw_client_secret = utils.get_or_create_m2m_client(cognito, gw_user_pool_id, CLIENT_NAME, RESOURCE_SERVER_ID, scope_names)

print(f"Gateway Client ID: {gw_client_id}")
print(f"Gateway Client Secret: {gw_client_secret}")

user_pool_domain = utils.get_or_create_user_pool_domain(cognito, gw_user_pool_id)

print(f"User Pool domain: {user_pool_domain}")

print(f"Gateway token url: https://{user_pool_domain['domain_name']}.auth.{REGION}.amazoncognito.com/oauth2/token")


# Get discovery URL
gw_cognito_discovery_url = f'https://cognito-idp.{REGION}.amazonaws.com/{gw_user_pool_id}/.well-known/openid-configuration'
print(gw_cognito_discovery_url)

# Create Amazon Cognito Pool for Inbound authorization to Runtime
USER_POOL_NAME = "sample-agentcore-runtime-pool"
RESOURCE_SERVER_ID = "sample-agentcore-runtime-id"
RESOURCE_SERVER_NAME = "sample-agentcore-runtime-name"
CLIENT_NAME = "sample-agentcore-runtime-client"
SCOPES = [
    {"ScopeName": "invoke",  # Just 'invoke', will be formatted as resource_server_id/invoke
    "ScopeDescription": "Scope for invoking the agentcore gateway"},
]

scope_names = [f"{RESOURCE_SERVER_ID}/{scope['ScopeName']}" for scope in SCOPES]
runtimeScopeString = " ".join(scope_names)


cognito = boto3.client("cognito-idp", region_name=REGION)

print("Creating or retrieving Cognito resources...")
runtime_user_pool_id = utils.get_or_create_user_pool(cognito, USER_POOL_NAME)
print(f"User Pool ID: {runtime_user_pool_id}")

utils.get_or_create_resource_server(cognito, runtime_user_pool_id, RESOURCE_SERVER_ID, RESOURCE_SERVER_NAME, SCOPES)
print("Resource server ensured.")

runtime_client_id, runtime_client_secret = utils.get_or_create_m2m_client(cognito, runtime_user_pool_id, CLIENT_NAME, RESOURCE_SERVER_ID, scope_names)

print(f"MCP runtime Client ID: {runtime_client_id}")
print(f"MCP runtime Secret: {runtime_client_secret}")


# Get discovery URL
runtime_cognito_discovery_url = f'https://cognito-idp.{REGION}.amazonaws.com/{runtime_user_pool_id}/.well-known/openid-configuration'
print(runtime_cognito_discovery_url)

# Create the Gateway    
gateway_client = boto3.client('bedrock-agentcore-control', region_name=REGION)
auth_config = {
    "customJWTAuthorizer": { 
        "allowedClients": [gw_client_id],  # Client MUST match with the ClientId configured in Cognito. Example: 7rfbikfsm51j2fpaggacgng84g
        "discoveryUrl": gw_cognito_discovery_url
    }
}
create_response = gateway_client.create_gateway(name='ac-gateway-mcp-server1',
    roleArn=agentcore_gateway_iam_role['Role']['Arn'], # The IAM Role must have permissions to create/list/get/delete Gateway
    protocolType='MCP',
    protocolConfiguration={
        'mcp': {
            'supportedVersions': ['2025-03-26'],
            'searchType': 'SEMANTIC'
        }
    },
    authorizerType='CUSTOM_JWT',
    authorizerConfiguration=auth_config,
    description='AgentCore Gateway with MCP Server target'
)
print(create_response)
# Retrieve the GatewayID used for GatewayTarget creation
gatewayID = create_response["gatewayId"]
gatewayURL = create_response["gatewayUrl"]
print(gatewayID)

# Create a sample MCP Server and host it in Runtime

from bedrock_agentcore_starter_toolkit import Runtime
from boto3.session import Session

boto_session = Session()
region = boto_session.region_name
print(f"Using AWS region: {region}")

required_files = ['mcp_server.py', 'requirements.txt']
for file in required_files:
    if not os.path.exists(file):
        raise FileNotFoundError(f"Required file {file} not found")
print("All required files found ✓")
agentcore_runtime = Runtime()

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
    entrypoint="mcp_server.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region=region,
    authorizer_configuration=auth_config,
    protocol="MCP",
    agent_name="ac_gateway_mcp_server"
)
print("Configuration completed ✓")

print("Launching MCP server to AgentCore Runtime...")
print("This may take several minutes...")
launch_result = agentcore_runtime.launch()

agent_arn = launch_result.agent_arn
agent_id = launch_result.agent_id

encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')

agent_url = f'https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT'
print("Launch completed ✓")
print(f"Agent ARN: {agent_arn}")
print(f"Agent ID: {agent_id}")


#Create an MCP server as target for AgentCore Gateway
#Configure Outbound Auth
#First, we will create an AgentCore Identity Resource Credential Provider for our AgentCore Gateway to use as outbound auth to our MCP server in AgentCore Runtime.
identity_client = boto3.client('bedrock-agentcore-control', region_name=REGION)

cognito_provider = identity_client.create_oauth2_credential_provider(
    name="ac-gateway-mcp-server-identity",
    credentialProviderVendor="CustomOauth2",
    oauth2ProviderConfigInput={
        'customOauth2ProviderConfig': {
            'oauthDiscovery': {
                'discoveryUrl': runtime_cognito_discovery_url,
            },
            'clientId': runtime_client_id,
            'clientSecret': runtime_client_secret
        }
    }
)
cognito_provider_arn = cognito_provider['credentialProviderArn']
print(cognito_provider_arn)


#Create the Gateway Target
gateway_client = boto3.client('bedrock-agentcore-control', region_name=REGION)
create_gateway_target_response = gateway_client.create_gateway_target(
    name='mcp-server-target',
    gatewayIdentifier=gatewayID,
    targetConfiguration={
        'mcp': {
            'mcpServer': {
                'endpoint': agent_url
            }
        }
    },
    credentialProviderConfigurations=[
        {
            'credentialProviderType': 'OAUTH',
            'credentialProvider': {
                'oauthCredentialProvider': {
                    'providerArn': cognito_provider_arn,
                    'scopes': [
                        runtimeScopeString
                    ]
                }
            }
        },
    ]
)
print(create_gateway_target_response)

# Check that the Gateway target exists, and is READY
list_targets_response = gateway_client.list_gateway_targets(gatewayIdentifier=gatewayID)
print(list_targets_response)


#Request the access token from Amazon Cognito for inbound authorization
print("Requesting the access token from Amazon Cognito authorizer...May fail for some time till the domain name propagation completes")
# token_response = utils.get_token(gw_user_pool_id, gw_client_id, gw_client_secret, scopeString, REGION)
# token = token_response["access_token"]
# print("Token response:", token)
