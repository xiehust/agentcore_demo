# legal-agent.py
import asyncio
import json
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
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional, Literal, AsyncGenerator, Union
from bedrock_agentcore import BedrockAgentCoreApp
from data_types import OperationsRequest
from dotenv import load_dotenv
import queue

import time
load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
)
# Initialize logger
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()
    
class StreamingQueue:
    """Simple async stream_queue for streaming responses."""
    
    def __init__(self,get_timeout=2):
        self._queue = asyncio.Queue()
        self._finished = False
        self._get_timeout = get_timeout
        
    async def put(self, item: str) -> None:
        """Add an item to the stream_queue."""
        if self._finished:
            self.reset()
        await self._queue.put(item)

    def reset(self) -> None:
        old_size = self._queue.qsize()
        self._finished = False
        # 清空队列
        cleared_count = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                cleared_count += 1
            except asyncio.QueueEmpty:
                break
            
    async def finish(self) -> None:
        """Mark the stream_queue as finished and add sentinel value."""
        self._finished = True
        await self._queue.put(None)

    async def stream(self):
        """Stream items from the stream_queue until finished."""
        while True:
            try:
                if self._get_timeout:
                    item = await asyncio.wait_for(
                        self._queue.get(), 
                        timeout=self._get_timeout
                    )
                else:
                    item = await self._queue.get()
                    
                if item is None and self._finished:
                    break
                yield item
            except asyncio.TimeoutError:
                # 可以选择继续等待或退出
                if self._finished:
                    break
                yield {"type":"heatbeat"}
                continue  


# Initialize streaming stream_queue
stream_queue = StreamingQueue()
# stream_queue = queue.Queue()

# Global variable to track current running agent task
current_agent_task: Optional[asyncio.Task] = None
     
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

async def process_query(prompt,options):
    text_started = False
    text_ended = False
    content_block_index = 0
    tool_results_dict ={}
    async for msg in query(prompt=prompt,options=options):
        # logger.info(msg)
        if isinstance(msg, UserMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print(f"User: {block.text}\n")
                elif isinstance(block, ToolResultBlock):
                    toolUseId = block.tool_use_id
                    if toolUseId in tool_results_dict:
                        tool_results = [tool_results_dict[toolUseId],
                                        {"tool_name":tool_results_dict[toolUseId]['name'],
                                        "tool_result": {"status":'success' if block.is_error is None else block.is_error  ,
                                                        "content":[{"text":json.dumps(block.content,ensure_ascii=False)}],
                                                        "toolUseId":toolUseId}
                                        }]
                        event = {'type':'result_pairs','data':{'stopReason':'tool_use','tool_results':tool_results}}
                        await stream_queue.put(event)
                    
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if not text_started:
                        text_started = True
                        event =  {'type': 'message_start', 'data': {'role': 'assistant'}}
                        await stream_queue.put(event)
                        
                    text_started = True
                    event = {'type': 'block_delta', 'data': {'delta': {'text': block.text }, 'contentBlockIndex': content_block_index}}
                    content_block_index += 1
                    await stream_queue.put(event)
                elif isinstance(block, ToolUseBlock):
                    if text_started:
                        text_started = False
                        event = {'type': 'block_stop', 'data': {'contentBlockIndex': content_block_index-1}}
                        await stream_queue.put(event)
                    
                    #save tool use id to global dict
                    
                    tool_results_dict[block.id] = {"name":block.name,"toolUseId":block.id,"input":block.input}
                    
                    event = {'type': 'block_start', 'data': {'start': {'toolUse': {'toolUseId': block.id, 'name': block.name}}, 'contentBlockIndex': content_block_index}}
                    content_block_index += 1
                    await stream_queue.put(event)
                    event = {'type': 'block_delta', 'data': {'delta': {'toolUse': {'input': block.input}}, 'contentBlockIndex': content_block_index}}
                    await stream_queue.put(event)
                    event = {'type': 'block_stop', 'data': {'contentBlockIndex': content_block_index}}
                    await stream_queue.put(event)
                    content_block_index += 1
                    
                    event = {'type': 'message_stop', 'data': {'stopReason': 'tool_use'}}
                    await stream_queue.put(event)
                    text_started = False
                    
                elif isinstance(block, ThinkingBlock):
                    if not text_started:
                        text_started = True
                        event =  {'type': 'message_start', 'data': {'role': 'assistant'}}
                        await stream_queue.put(event)
                        
                    text_started = True
                    event = {'type': 'block_delta', 'data': {'delta': {'reasoningContent': {'text': block.thinking}}, 'contentBlockIndex': content_block_index}}
                    await stream_queue.put(event)
                    event = {'type': 'block_stop', 'data': {'contentBlockIndex': content_block_index}}
                    await stream_queue.put(event)
                    content_block_index += 1
                    print(f"Thinking: {block.thinking}\n")
            
        elif isinstance(msg, SystemMessage):
            # Ignore system messages
            pass
        elif isinstance(msg, ResultMessage):
            await stream_queue.put(f"\n\nComplete. Total usage: ${msg.usage}\nTotal cost: ${msg.total_cost_usd:.4f}")
            await stream_queue.put({'type': 'message_stop', 'data': {'stopReason': 'end_turn'}})
        
    
    
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
            # cwd="/app/workspace"
        )
        # Monitor tool usage and responses
        await process_query(prompt=prompt,options=options)
        
    except asyncio.CancelledError:
        logger.info("Agent task was cancelled")
        await stream_queue.put({"type": "stopped"})
        raise  # Re-raise to properly propagate cancellation
    except CLINotFoundError:
        print("Install CLI: npm install -g @anthropic-ai/claude-code")
        await stream_queue.put("Install CLI: npm install -g @anthropic-ai/claude-code")
    except ProcessError as e:
        print(f"Process error: {e}")
        await stream_queue.put(f"Process error: {e}")
    except CLIConnectionError as e:
        print(f"CLI connection error: {e}")
        await stream_queue.put(f"CLI connection error: {e}")
    except CLIJSONDecodeError as e:
        print(f"CLI JSON decode error: {e}")
        await stream_queue.put(f"CLI JSON decode error: {e}")
    except ConnectionError as e:
        print(f"Connection error: {e}")
        await stream_queue.put(f"Connection error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
        await stream_queue.put(f"Unexpected error: {e}")
    finally:
        await stream_queue.finish()


async def pull_queue_stream(model):
    current_content = ""
    thinking_start = False
    thinking_text_index = 0
    tooluse_start = False
    async for item in stream_queue.stream():
        event_data = {
            "id": f"chat{time.time_ns()}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": None
            }]
        }
        # Handle string items (error messages, completion messages, etc.)
        if isinstance(item, str) or not item:
            # yield f"data: {json.dumps(event_data)}\n\n"
            continue
        # Handle dictionary items (event objects)
        if not isinstance(item, dict) or "type" not in item:
            continue
        
        if item["type"] == "heatbeat":
            await asyncio.sleep(0.001)
            yield f": heartbeat\n\n"  
            continue
        
        # 处理不同的事件类型
        elif item["type"] == "message_start":
            event_data["choices"][0]["delta"] = {"role": "assistant"}
            
        elif item["type"] == "block_start":
            block_start = item["data"]
            if "toolUse" in block_start.get("start", {}):
                event_data["choices"][0]["message_extras"] = {
                    "tool_name": block_start["start"]["toolUse"]["name"]
                }
            
        elif item["type"] == "block_delta":
            if "text" in item["data"]["delta"]:
                text = ""
                text += str(item["data"]["delta"]["text"])
                current_content += text
                event_data["choices"][0]["delta"] = {"content": text}
                thinking_text_index = 0
                
            if "toolUse" in item["data"]["delta"]:
                if not tooluse_start:    
                    tooluse_start = True
                event_data["choices"][0]["delta"] = {"toolinput_content": json.dumps(item["data"]["delta"]["toolUse"]['input'],ensure_ascii=False)}
                
            if "reasoningContent" in item["data"]["delta"]:
                if 'text' in item["data"]["delta"]["reasoningContent"]:
                    event_data["choices"][0]["delta"] = {"reasoning_content": item["data"]["delta"]["reasoningContent"]["text"]}

        elif item["type"] == "block_stop":
            if tooluse_start:
                tooluse_start = False
                event_data["choices"][0]["delta"] = {"toolinput_content": "<END>"}
        
        elif item["type"] in [ "message_stop" ,"result_pairs"]:
            event_data["choices"][0]["finish_reason"] = item["data"]["stopReason"]
            if item["data"].get("tool_results"):
                event_data["choices"][0]["message_extras"] = {
                    "tool_use": json.dumps(item["data"]["tool_results"],ensure_ascii=False)
                }
            if item["data"]["stopReason"] == 'end_turn':
                event_data = {
                    "id": f"stop{time.time_ns()}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model, 
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "end_turn"
                    }]
                }     
                # endturn
                # logger.info(event_data)
                yield f"data: [DONE]\n\n"     
                break
                # return
        # 发送事件
        # logger.info(event_data)
        yield f"data: {json.dumps(event_data)}\n\n"
        
        # 手动停止流式响应
        if item["type"] == "stopped":
            event_data = {
                "id": f"stop{time.time_ns()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop_requested"
                }]
            }
            yield f"data: {json.dumps(event_data)}\n\n"
            yield f"data: [DONE]\n\n"
            break
            # return

        
@app.entrypoint
async def agent_invocation(payload:OperationsRequest):
    global current_agent_task
    request = OperationsRequest(**payload)
    
    user_id = request.user_id
    data = request.data
    logger.info(f"=====NEW REQUEST START: type:{request.request_type}, user_id:{user_id}=======")
    logger.info(f"=====request data:{data}=======\n")
    prompt = ""
    if request.request_type == 'chatcompletion':
        if stream_queue._queue.qsize() > 0 or stream_queue._finished:
            stream_queue.reset()
            
        model = data.model
        msg = data.messages[-1]
        if isinstance(msg.content, str):
            prompt = msg.content
        else:
            content_item = msg.content[0]
            if content_item.type == "text":
                prompt = content_item.text
        if prompt:
            # Create and start the agent task
            task = asyncio.create_task(agent_task(prompt=prompt))
            current_agent_task = task  # Store reference to current task
            
            async def stream_with_task():
                """Stream results while ensuring task completion."""
                try:
                    async for item in pull_queue_stream(model):
                        yield item
                        logger.info(item)
                    await task
                except asyncio.CancelledError:
                    logger.info("Agent task was cancelled")
                    # Don't re-raise, just complete the stream gracefully
                finally:
                    current_agent_task = None  # Clear task reference when done

            return stream_with_task()
    elif request.request_type == 'stopstream':
        # Stop agent_task
        logger.info("=====STOP STREAM REQUEST RECEIVED=======")
        if current_agent_task and not current_agent_task.done():
            logger.info("Cancelling current agent task")
            current_agent_task.cancel()
            # Add stopped event to queue to trigger proper stream termination
            await stream_queue.put({"type": "stopped"})
            logger.info("Agent task cancellation requested")
        else:
            logger.info("No active agent task to cancel")
        
        # Return a simple response indicating stop was processed
        return {"status": "success", "message": "Stream stop requested"}
    elif request.request_type == 'removehistory':
        logger.info("=====REMOVE HISTORY REQUEST RECEIVED=======")
        return {"status": "success", "message": "Remove history requested"}
        

if __name__ == "__main__":
    app.run()