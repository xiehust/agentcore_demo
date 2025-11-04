import boto3
from boto3.session import Session
import os
from dotenv import load_dotenv
load_dotenv()

# Get boto session
boto_session = Session()
REGION = boto_session.region_name

runtime_client_id= os.environ.get("runtime_client_id")
runtime_client_secret= os.environ.get("runtime_client_secret")
runtime_cognito_discovery_url= os.environ.get("runtime_cognito_discovery_url")

assert runtime_client_id is not None, "runtime_client_id is None"
assert runtime_client_secret is not None, "runtime_client_secret is None"
assert runtime_cognito_discovery_url is not None, "runtime_cognito_discovery_url is None"

identity_client = boto3.client('bedrock-agentcore-control', region_name=REGION)

#Configure Outbound Auth
cognito_provider = identity_client.create_oauth2_credential_provider(
    name="ac-quip-mcp-server-identity",
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
    name='quip-mcp-server-target',
    gatewayIdentifier=os.environ.get("gatewayID"),
    targetConfiguration={
        'mcp': {
            'mcpServer': {
                'endpoint': os.environ.get("runtimeURL")
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
                        "sample-agentcore-runtime-id/invoke"
                    ]
                }
            }
        },
    ]
)
print(create_gateway_target_response)

# Check that the Gateway target exists, and is READY
list_targets_response = gateway_client.list_gateway_targets(gatewayIdentifier=os.environ.get("gatewayID"))
print(list_targets_response['items'])
