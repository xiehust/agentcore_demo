import boto3
import os
# Create a client
client = boto3.client('bedrock-agentcore-control')

# Get agent runtime status
response = client.get_agent_runtime(
    agentRuntimeId='agent_runtime-dLZqBT4IoJ',
    agentRuntimeVersion='15'  # Replace with your version
)

# The status field shows the overall runtime status (CREATING, READY, etc.)
print(f"Runtime status: {response['status']}")

agent_arn= "arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_runtime-dLZqBT4IoJ"
encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')
region = "us-west-2"
url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocasions?qualifier=DEFAULT"
print(url)
