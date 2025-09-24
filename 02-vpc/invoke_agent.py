import boto3
import json

agent_core_client = boto3.client('bedrock-agentcore', region_name='us-west-2')
payload = json.dumps({"user_input": "What is Bedrock AgentCore?"})

response = agent_core_client.invoke_agent_runtime(
    agentRuntimeArn='arn:aws:bedrock-agentcore:us-west-2:xxx:runtime/vpc_strands_agent2-xxx',
    runtimeSessionId='dfmeoagmreaklgmrkleafremoigrmtesogmtrskhmtkrlshm1',  # Must be 33+ chars
    payload=payload,
    qualifier="DEFAULT"
)
# Process and print the response
if "text/event-stream" in response.get("contentType", ""):
  
    # Handle streaming response
    content = []
    for line in response["response"].iter_lines(chunk_size=10):
        if line:
            line = line.decode("utf-8")
            if line.startswith("data: "):
                line = line[6:]
                data = json.loads(line)
                print(data,end="")
                content.append(line)
    # print("\nComplete response:", "\n".join(content))

elif response.get("contentType") == "application/json":
    # Handle standard JSON response
    content = []
    for chunk in response.get("response", []):
        content.append(chunk.decode('utf-8'))
    print(json.loads(''.join(content)))
  
else:
    # Print raw response for other content types
    print(response)