import logging
import json
import base64
import hmac
import hashlib

from boto3.session import Session
from uuid import uuid4
from urllib.parse import quote

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

from helpers.utils import get_cognito_secret, reauthenticate_user, get_ssm_parameter, SSM_DOCS_AGENT_ARN, SSM_BLOGS_AGENT_ARN

from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from fastapi import HTTPException


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 300  # set request timeout to 5 minutes
username = "testuser"
MCP_AGENT_ARN = get_ssm_parameter(SSM_DOCS_AGENT_ARN)
BLOG_AGENT_ARN = get_ssm_parameter(SSM_BLOGS_AGENT_ARN)
boto_session = Session()
region = boto_session.region_name


app = BedrockAgentCoreApp()


def create_message(*, role: Role = Role.user, text: str) -> Message:
    return Message(
        kind="message",
        role=role,
        parts=[Part(TextPart(kind="text", text=text))],
        message_id=uuid4().hex,
    )

async def send_sync_message(message: str, agent_arn: str):
    escaped_agent_arn = quote(agent_arn, safe='')
    runtime_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_agent_arn}/invocations/"
    
    session_id = str(uuid4())
    secret = json.loads(get_cognito_secret())
    bearer_token = reauthenticate_user(secret.get("client_id"), secret.get("client_secret"))
    
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': session_id
    }
        
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=headers) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=runtime_url)
        agent_card = await resolver.get_agent_card()

        config = ClientConfig(httpx_client=httpx_client, streaming=False)
        factory = ClientFactory(config)
        client = factory.create(agent_card)

        msg = create_message(text=message)

        async for event in client.send_message(msg):
            if isinstance(event, Message):
                return event
            elif isinstance(event, tuple) and len(event) == 2:
                return event[0]
            else:
                return event

@tool
async def send_mcp_message(message: str):
    return await send_sync_message(message, MCP_AGENT_ARN)


@tool
async def send_blog_message(message: str):
    return await send_sync_message(message, BLOG_AGENT_ARN)


system_prompt = """You are an orchestrator, that will invoke other agents to get information about:

- AWS Documentation: Most recent data on AWS docs, from AWS Docs MCP
- AWS Blogs and News: Agent that query in the web for latest AWS Blogs and news

Key capabilities:
- Search and retrieve information from AWS service documentation on AWS Documentation Tool
- Search and retrieve information from AWS Blogs and News if asked for a specific subject

Considerations:

- all your queries should be summarized and optimized
- ask underlying agents/tools to summarize and shorten answers
- If you're unsure about something, clearly state your limitations
- Try to simplify/summarize answers to make it faster, small and objective

"""

agent = Agent(system_prompt=system_prompt, 
              tools=[send_mcp_message, send_blog_message],
              name="AWS Orchestration Agent",
              description="An agent to orchestrate sub-agents")

@app.entrypoint
async def invoke_agent(payload, context):
    logger.info("Received invocation request")

    logger.info(f"Payload: {payload}")
    logger.info(f"Context: {context}")
    try:

        # Extract user prompt
        user_prompt = payload.get("prompt", "")
        if not user_prompt:
            raise HTTPException(
                status_code=400,
                detail="No prompt found in input. Please provide a 'prompt' key in the input.",
            )
        
        session_id = context.session_id
        
        logger.info(f"Processing query: {user_prompt}")

        # Get the agent stream
        agent_stream = agent.stream_async(user_prompt)

        async for event in agent_stream:
            yield event

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent processing failed: {e}")
        logger.exception("Full exception details:")
        raise HTTPException(
            status_code=500, detail=f"Agent processing failed: {str(e)}"
        )

if __name__ == "__main__":
    app.run()