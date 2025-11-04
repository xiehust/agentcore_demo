import os
import json
import requests
import boto3
from boto3.session import Session
from strands.tools import tool
from dotenv import load_dotenv
load_dotenv()

# Get boto session
boto_session = Session()
region = boto_session.region_name

from helpers.utils import create_agentcore_runtime_execution_role, ORCHESTRATOR_ROLE_NAME

agent_name="aws_orchestrator_assistant"

execution_role_arn = create_agentcore_runtime_execution_role(ORCHESTRATOR_ROLE_NAME)

from bedrock_agentcore_starter_toolkit import Runtime

agentcore_runtime = Runtime()

# Configure the deployment
response = agentcore_runtime.configure(
    entrypoint="agents/orchestrator.py",
    execution_role=execution_role_arn,
    auto_create_ecr=True,
    requirements_file="agents/requirements.txt",
    region=region,
    agent_name=agent_name,
    authorizer_configuration={
        "customJWTAuthorizer": {
            "allowedClients": [os.environ.get("COGNITO_CLIENT_ID")],
            "discoveryUrl": os.environ.get("discovery_url"),
        }
    },
)

print("Configuration completed:", response)

launch_result = agentcore_runtime.launch()
print("Launch completed:", launch_result.agent_arn)

agent_arn = launch_result.agent_arn
print("Agent ARN:", agent_arn)

import time

# Wait for the agent to be ready
status_response = agentcore_runtime.status()
status = status_response.endpoint["status"]

end_status = ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]
while status not in end_status:
    print(f"Waiting for deployment... Current status: {status}")
    time.sleep(10)
    status_response = agentcore_runtime.status()
    status = status_response.endpoint["status"]

print(f"Final status: {status}")

ORCHESTRATION_ID = launch_result.agent_id
ORCHESTRATION_ARN = launch_result.agent_arn
ORCHESTRATION_NAME = agent_name

print(f"ORCHESTRATION Agent ID: {ORCHESTRATION_ID}\n"
      f"ORCHESTRATION_ARN:{ORCHESTRATION_ARN}\n"
      f"ORCHESTRATION_NAME:{ORCHESTRATION_NAME}")