# Import LangGraph and LangChain components
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langgraph.prebuilt   import create_react_agent
# Import the AgentCoreMemorySaver that we will use as a checkpointer
import os
import logging


region = os.getenv('AWS_REGION', 'us-west-2')
logging.getLogger("math-agent").setLevel(logging.DEBUG)




@tool
def add(a: int, b: int):
    """Add two integers and return the result"""
    return a + b


@tool
def multiply(a: int, b: int):
    """Multiply two integers and return the result"""
    return a * b

MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

from langgraph_checkpoint_aws import AgentCoreMemorySaver
from bedrock_agentcore.memory import MemoryClient
# Create or get the memory resource
memory_name = "MathLanggraphAgent"
client = MemoryClient(region_name=region)
memory = client.create_or_get_memory(name=memory_name)
memory_id = memory['id'] # Keep this memory ID for later use

# Initialize checkpointer for state persistence
checkpointer = AgentCoreMemorySaver(memory_id, region_name=region)

# Initialize LLM
llm = init_chat_model(MODEL_ID, model_provider="bedrock_converse", region_name=region)

graph = create_react_agent(
    model=llm,
    tools=[add, multiply],
    prompt="You are a helpful assistant",
    checkpointer=checkpointer,
)

config = {
    "configurable": {
        "thread_id": "session-1", # REQUIRED: This maps to Bedrock AgentCore session_id under the hood
        "actor_id": "react-agent-1", # REQUIRED: This maps to Bedrock AgentCore actor_id under the hood
    }
}

inputs = {"messages": [{"role": "user", "content": "What is 1337 times 515321? Then add 412 and return the value to me."}]}

for chunk in graph.stream(inputs, stream_mode="updates", config=config):
    if 'agent' in chunk:
        if 'messages' in chunk['agent']:
            print(chunk['agent']['messages'][0].content)
