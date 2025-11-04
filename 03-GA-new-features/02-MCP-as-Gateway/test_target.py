import boto3

client = boto3.client('bedrock-agentcore-control')

response = client.get_gateway_target(
    gatewayIdentifier='',
    targetId='H7YTFPFJTC'
)

print(response)