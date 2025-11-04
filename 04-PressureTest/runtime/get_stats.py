import boto3
import json

client = boto3.client('bedrock-agentcore', region_name='us-west-2')
payload = json.dumps({
    "input": {"get_stats":True}
})

response = client.invoke_agent_runtime(
    agentRuntimeArn='arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-M09jI18WJo',
    runtimeSessionId='agentcore-load-test-session-12345',  # Must be 33+ chars
    payload=payload,
    qualifier="DEFAULT" # Optional
)
response_body = response['response'].read()
response_data = json.loads(response_body)
print("Agent Response:", response_data)


response = client.invoke_agent_runtime(
    agentRuntimeArn='arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry_2-d7c7qaF16U',
    runtimeSessionId='agentcore-load-test-session-12345',  # Must be 33+ chars
    payload=payload,
    qualifier="DEFAULT" # Optional
)
response_body = response['response'].read()
response_data = json.loads(response_body)
print("Agent Response:", response_data)