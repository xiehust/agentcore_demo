import asyncio
import logging
import os
from uuid import uuid4
import json
import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart
from helpers.utils import  reauthenticate_user
import requests
from urllib.parse import quote
from dotenv import load_dotenv
load_dotenv()
region = 'us-west-2'
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # set request timeout to 5 minutes

bearer_token = reauthenticate_user(
    os.environ.get("COGNITO_CLIENT_ID"),
    os.environ.get("COGNITO_CLIENT_SECRET")
)

def fetch_agent_card(agent_arn):
    # URL encode the agent ARN
    escaped_agent_arn = quote(agent_arn, safe='')

    # Construct the URL
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_agent_arn}/invocations/.well-known/agent-card.json"
    logger.info(url)
    # Generate a unique session ID
    session_id = str(uuid4())
    logger.info(f"Generated session ID: {session_id}")

    # Set headers
    headers = {
        'Accept': '*/*',
        'Authorization': f'Bearer {bearer_token}',
        'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': session_id,
        'X-Amzn-Trace-Id': f'aws_docs_assistant_{session_id}'
    }

    try:
        # Make the request
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        # Parse and pretty print JSON
        agent_card = response.json()
        logger.info(json.dumps(agent_card, indent=2))

        return agent_card

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching agent card: {e}")
        return None
    
    
def format_agent_response(response):
    """Extract and format agent response for human readability."""
    # Get the main response text from artifacts
    if response.artifacts and len(response.artifacts) > 0:
        artifact = response.artifacts[0]
        if artifact.parts and len(artifact.parts) > 0:
            return artifact.parts[0].root.text
    
    # Fallback: concatenate all agent messages from history
    agent_messages = [
        msg.parts[0].root.text 
        for msg in response.history 
        if msg.role.value == 'agent' and msg.parts
    ]
    return ''.join(agent_messages)


def create_message(*, role: Role = Role.user, text: str) -> Message:
    return Message(
        kind="message",
        role=role,
        parts=[Part(TextPart(kind="text", text=text))],
        message_id=uuid4().hex,
    )

async def send_sync_message(agent_arn, message: str):
    # Get runtime URL from environment variable
    escaped_agent_arn = quote(agent_arn, safe='')

    # Construct the URL
    runtime_url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped_agent_arn}/invocations/"
    
    # Generate a unique session ID
    session_id = str(uuid4())
    print(f"Generated session ID: {session_id}")

    # Add authentication headers for AgentCore
    headers = {"Authorization": f"Bearer {bearer_token}",
              'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': session_id}
        
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=headers) as httpx_client:
        # Get agent card from the runtime URL
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=runtime_url)
        agent_card = await resolver.get_agent_card()
        print(agent_card)

        # Agent card contains the correct URL (same as runtime_url in this case)
        # No manual override needed - this is the path-based mounting pattern

        # Create client using factory
        config = ClientConfig(
            httpx_client=httpx_client,
            streaming=False,  # Use non-streaming mode for sync response
        )
        factory = ClientFactory(config)
        client = factory.create(agent_card)

        # Create and send message
        msg = create_message(text=message)

        # With streaming=False, this will yield exactly one result
        async for event in client.send_message(msg):
            if isinstance(event, Message):
                logger.info(event.model_dump_json(exclude_none=True, indent=2))
                return event
            elif isinstance(event, tuple) and len(event) == 2:
                # (Task, UpdateEvent) tuple
                task, update_event = event
                logger.info(f"Task: {task.model_dump_json(exclude_none=True, indent=2)}")
                if update_event:
                    logger.info(f"Update: {update_event.model_dump_json(exclude_none=True, indent=2)}")
                return task
            else:
                # Fallback for other response types
                logger.info(f"Response: {str(event)}")
                return event

def format_agent_trace(response):
    """Format agent response as a readable trace of calls."""
    print("=" * 60)
    print("🔍 AGENT EXECUTION TRACE")
    print("=" * 60)
    
    # Context info
    print(f"📋 Context ID: {response.context_id}")
    print(f"🆔 Task ID: {response.id}")
    print(f"📊 Status: {response.status.state.value}")
    print(f"⏰ Completed: {response.status.timestamp}")
    print()
    
    # Trace through history
    print("🔄 EXECUTION FLOW:")
    print("-" * 40)
    
    for i, msg in enumerate(response.history, 1):
        role_icon = "👤" if msg.role.value == "user" else "🤖"
        text = msg.parts[0].root.text if msg.parts else "[No content]"
        
        # Truncate long messages for trace view
        if len(text) > 80:
            text = text[:77] + "..."
            
        print(f"{i:2d}. {role_icon} {msg.role.value.upper()}: {text}")
    
    print()
    print("✅ FINAL RESULT:")
    print("-" * 40)
    
    # Final artifact
    if response.artifacts:
        final_text = response.artifacts[0].parts[0].root.text
        print(final_text[:200] + "..." if len(final_text) > 200 else final_text)
    
    print("=" * 60)
    
    
if __name__ == "__main__":
    docs_agent_arn = "arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/aws_docs_assistant-iwuMfbEWkv"
    blog_agent_arn = "arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/aws_blog_assistant-P22SqL8p6O"
    
    print(fetch_agent_card(docs_agent_arn))
    print(fetch_agent_card(blog_agent_arn))
    
    # Example usage
    message = "What is the capital of France?"
    response = asyncio.run(send_sync_message(docs_agent_arn, message))
    print(format_agent_response(response))



    result = asyncio.run(send_sync_message(blog_agent_arn, "Give me the latest published blog for Bedrock AgentCore?"))
    formatted_output = format_agent_response(result)
    print(formatted_output)
    format_agent_trace(result)