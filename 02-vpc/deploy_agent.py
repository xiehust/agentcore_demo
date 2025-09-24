import boto3
import time

RUNTIME_NAME = 'vpc_strands_agent2'
subnets = ['subnet-08629b2ddf6d4fd30', 'subnet-00f3f3d052d492051']
sgs = ['sg-0b135195c2e36643f']


sts = boto3.client('sts')
account_id = sts.get_caller_identity()['Account']

client = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
response = client.list_agent_runtimes(maxResults=100)

runtime_id= None
for runtime in response['agentRuntimes']:
    # print(runtime)
    if runtime['agentRuntimeName'] == RUNTIME_NAME:
        runtime_id = runtime['agentRuntimeId']
        break
        
print(f"Found existing runtime_id:{runtime_id}")
if not runtime_id:
    response = client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        agentRuntimeArtifact={
            'containerConfiguration': {
                'containerUri': f'{account_id}.dkr.ecr.us-west-2.amazonaws.com/vpc-strands-agent:latest'
            }
        },
        # networkConfiguration={"networkMode": "PUBLIC"},
        networkConfiguration={
            'networkMode': 'VPC',
            'networkModeConfig': {
                'subnets': subnets,
                'securityGroups': sgs
            }
        },
        roleArn=f'arn:aws:iam::{account_id}:role/vpc_strands_agent'
    )
    print(f"Agent Runtime created successfully!")
    print(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
    print(f"Response: {response}")
else:
    response = client.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact={
            'containerConfiguration': {
                'containerUri': f'{account_id}.dkr.ecr.us-west-2.amazonaws.com/vpc-strands-agent:latest'
            }
        },
        # networkConfiguration={"networkMode": "PUBLIC"},
        networkConfiguration={
            'networkMode': 'VPC',
            'networkModeConfig': {
                'subnets': subnets,
                'securityGroups':sgs
            }
        },
        roleArn=f'arn:aws:iam::{account_id}:role/vpc_strands_agent'
    )
    print(f"Agent Runtime update successfully!")
    print(f"Agent Runtime ARN: {response['agentRuntimeArn']}")
    print(f"Response: {response}")

runtime_id = response['agentRuntimeId']

while True:
    response2 = client.get_agent_runtime(
        agentRuntimeId=runtime_id)
    status = response2['status']
    print(f"Status: {status}")
    if status not in ['CREATING','UPDATING']:
        break
    time.sleep(10)


