import asyncio
import logging
import os
from uuid import uuid4
import json
from helpers.utils import  reauthenticate_user
import requests
from urllib.parse import quote
from dotenv import load_dotenv
load_dotenv()
region = 'us-west-2'
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # set request timeout to 5 minutes
ORCHESTRATION_ARN = "arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/aws_orchestrator_assistant-bdzco724nc"
bearer_token = reauthenticate_user(
    os.environ.get("COGNITO_CLIENT_ID"),
    os.environ.get("COGNITO_CLIENT_SECRET")
)


import requests
import json
import uuid
from urllib.parse import quote

headers = {
    'Authorization': f'Bearer {bearer_token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': str(uuid.uuid4())
}

prompts = [{"prompt": "What is DynamoDB?"},
          {"prompt": "Give me the latest published blog for Bedrock AgentCore?"}]

escaped_agent_arn = quote(ORCHESTRATION_ARN, safe='')

for prompt in prompts:
    print(f"===========test==========")
    print(f"Prompt: {prompt['prompt']}")
    response = requests.post(
        f'https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_agent_arn}/invocations',
        headers=headers,
        data=json.dumps(prompt)
    )

    for line in response.iter_lines(decode_unicode=True):
        if line.startswith('data: '):
            data = line[6:]
            try:
                parsed = json.loads(data)
                print(parsed)
            except:
                print(data)