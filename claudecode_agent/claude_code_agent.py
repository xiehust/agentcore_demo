# legal-agent.py
import asyncio
import os
import boto3
from botocore.exceptions import ClientError
from claude_code_sdk import CLINotFoundError, ProcessError,CLIJSONDecodeError,CLIConnectionError
from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query
)
from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv
load_dotenv()


app = BedrockAgentCoreApp()

class StreamingQueue:
    """Simple async queue for streaming responses."""
    
    def __init__(self):
        self._queue = asyncio.Queue()
        self._finished = False

    async def put(self, item: str) -> None:
        """Add an item to the queue."""
        await self._queue.put(item)

    async def finish(self) -> None:
        """Mark the queue as finished and add sentinel value."""
        self._finished = True
        await self._queue.put(None)

    async def stream(self):
        """Stream items from the queue until finished."""
        while True:
            item = await self._queue.get()
            if item is None and self._finished:
                break
            yield item


# Initialize streaming queue
queue = StreamingQueue()


async def display_message(msg):
    """Standardized message display function.

    - UserMessage: "User: <content>"
    - AssistantMessage: "Claude: <content>"
    - SystemMessage: ignored
    - ResultMessage: "Result ended" + cost if available
    """
    if isinstance(msg, UserMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"User: {block.text}\n")
                await queue.put(f"User: {block.text}\n")
            elif isinstance(block, ToolResultBlock):
                print(f"**Tool invoke success:** {block.is_error}\n")
                print(f"**Tool result content:** {block.content}\n")
                await queue.put(f"**Tool invoke success:** {block.is_error}\n")
                await queue.put(f"**Tool result content:** {block.content}\n")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"Claude: {block.text}\n")
                await queue.put(f"Claude: {block.text}\n")
            elif isinstance(block, ToolUseBlock):
                print(f"Used tool: {block.name}  -> {block.input}\n")
                await queue.put(f"**Used tool:** {block.name}  -> {block.input}\n")
            elif isinstance(block, ThinkingBlock):
                print(f"Thinking: {block.thinking}\n")
                await queue.put(f"Thinking: {block.thinking}\n")
        
    elif isinstance(msg, SystemMessage):
        # Ignore system messages
        pass
    elif isinstance(msg, ResultMessage):
        print(f"\n\nComplete. Total usage: ${msg.usage}")
        print(f"\nTotal cost: ${msg.total_cost_usd:.4f}")
        await queue.put(f"\n\nComplete. Total usage: ${msg.usage}")
        await queue.put(f"\nTotal cost: ${msg.total_cost_usd:.4f}")
        
        
def get_aws_account_id():
    """Get AWS account ID from STS."""
    try:
        sts = boto3.client('sts')
        return sts.get_caller_identity()['Account']
    except Exception as e:
        print(f"Error getting AWS account ID: {e}")
        return None

def get_prebuilt_mcp_servers():
    """Get MCP servers configuration with dynamic S3 bucket creation."""
    # Get region from environment variable, default to us-west-2
    region = os.getenv('AWS_DEFAULT_REGION', 'us-west-2')
    
    # Get AWS account ID
    account_id = get_aws_account_id()
    if not account_id:
        raise Exception("Unable to get AWS account ID")
    
    # Generate bucket name
    bucket_name = f"eb-deploy-{region}-{account_id}"
    
    servers = {
        "elastic_beanstalk": {
            "command": "uv",
            "args": [
                "--directory", "/app/mcp",
                "run", "eb_server.py"
            ],
            "env": {
                "region": region,
                "s3_bucket_name": bucket_name
            }
        }
    }
    
    if ctx7_key:= os.getenv('CONTEXT7_API_KEY'):
        servers = { **servers, 
                     "context7": {
                         "type": "http",
                        "url": "https://mcp.context7.com/mcp",
                        "headers": {
                            "CONTEXT7_API_KEY": ctx7_key
                        }
                        }
                   }
    return servers



DEFAULT_SYSTEM = """You are an expert web application developer specializing in AWS Elastic Beanstalk deployments. Your primary responsibilities include:

## Working Environment
- **Working Directory**: You are restricted to `/app/workspace/` as your base working directory
- All project files, configurations, and deployments must be created within this directory structure
- Maintain organized project structure within this workspace

## Core Tasks
- Build, configure, and deploy web applications to AWS Elastic Beanstalk
- Utilize Model Context Protocol (MCP) tools effectively for development workflows
- Leverage context7 MCP tools to analyze, validate, and manage project dependencies
- Ensure proper application configuration for production environments

## Technical Requirements
- Verify all dependencies are correctly specified in requirements files
- Configure appropriate runtime environments and platform versions
- Follow AWS Elastic Beanstalk best practices for scalability and security
- Use Flask to build the web server

## Port Configuration Requirements
- **Python platforms**: Applications must run on port 8000 (default nginx upstream)
- Always use environment variable `PORT` when available: `os.environ.get('PORT', 8000)`
- Configure applications to bind to `0.0.0.0` as appropriate
- Ensure nginx proxy configuration matches application port (typically handled automatically)

## MCP Tool Usage
- Use context7 MCP tools to maintain accurate dependency tracking
- Validate configuration files (Procfile, .ebextensions, etc.) before deployment
- Ensure version compatibility across all project components
"""


async def agent_task(prompt,system=None,model=None,mcp_configs=None,allowed_tools=[]):
    try:
        # Get MCP servers configuration with dynamic bucket creation
        mcp_servers = get_prebuilt_mcp_servers()
        
        if mcp_configs and 'mcpServers' in mcp_configs:
            mcp_servers.update(mcp_configs)
        
        options=ClaudeCodeOptions(
            model= model if model else "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
            mcp_servers=mcp_servers,
            allowed_tools=["mcp__elastic_beanstalk", "mcp__context7","Read", "Write","TodoWrite","Task","LS","Bash","Edit","Grep","Glob"]+allowed_tools,
            disallowed_tools=["Bash(rm*)","KillBash"],
            permission_mode='acceptEdits',
            append_system_prompt =  system if system else DEFAULT_SYSTEM, 
            max_turns=100,
            cwd="/app/workspace"
        )
        # Monitor tool usage and responses
        async for message in query(prompt=prompt,options=options):
            await display_message(message)
                
    except CLINotFoundError:
        print("Install CLI: npm install -g @anthropic-ai/claude-code")
        await queue.put("Install CLI: npm install -g @anthropic-ai/claude-code")
    except ProcessError as e:
        print(f"Process error: {e}")
        await queue.put(f"Process error: {e}")
    except CLIConnectionError as e:
        print(f"CLI connection error: {e}")
        await queue.put(f"CLI connection error: {e}")
    except CLIJSONDecodeError as e:
        print(f"CLI JSON decode error: {e}")
        await queue.put(f"CLI JSON decode error: {e}")
    except ConnectionError as e:
        print(f"Connection error: {e}")
        await queue.put(f"Connection error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
        await queue.put(f"Unexpected error: {e}")
    finally:
        await queue.finish()

@app.entrypoint
async def agent_invocation(payload):
    user_message = payload.get(
        "prompt", 
        "No prompt found in input, please guide customer to create a JSON payload with prompt key"
    )
    system_message = payload.get("system")
    model = payload.get("model")
    mcp_configs = payload.get("mcp_configs")
    allowed_tools = payload.get("allowed_tools",[])
    # Create and start the agent task
    task = asyncio.create_task(agent_task(prompt=user_message,
                                          system=system_message,
                                          model=model,
                                          mcp_configs=mcp_configs,
                                          allowed_tools=allowed_tools))
    
    async def stream_with_task():
        """Stream results while ensuring task completion."""
        async for item in queue.stream():
            yield item
        await task  # Ensure task completes
    
    return stream_with_task()
    
if __name__ == "__main__":
    app.run()