import os
import boto3
from boto3.session import Session
from dotenv import load_dotenv
from time import sleep
load_dotenv()

# Get boto session
boto_session = Session()
REGION = boto_session.region_name



# Update the Gateway to use Amazon Fedarate
gateway_client = boto3.client('bedrock-agentcore-control', region_name=REGION)
auth_config = {
    "customJWTAuthorizer": { 
        "allowedAudience": [""], 
         "discoveryUrl": ""
    }
}
create_response = gateway_client.create_gateway(
    name="ac-gateway-midway",
    authorizerType="CUSTOM_JWT",
    protocolType="MCP",
    authorizerConfiguration=auth_config,
    roleArn="arn:aws:iam::434444145045:role/agentcore-sample-mcpgateway-role",
    description="demo gateway with midway auth",
    
)
sleep(3)

print(create_response)
gatewayID = create_response["gatewayId"]
gatewayURL = create_response["gatewayUrl"]
print(f"Gateway created with ID: {gatewayID}")
print(f"Gateway URL: {gatewayURL}")

cognito_provider_arn = ""
create_gateway_target_response = gateway_client.create_gateway_target(
    name='quip-mcp-server-target',
    gatewayIdentifier=gatewayID,
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