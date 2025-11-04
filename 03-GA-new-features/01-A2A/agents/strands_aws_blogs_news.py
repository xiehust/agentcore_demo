import logging
import os
from strands_tools.calculator import calculator
from strands import Agent, tool
from strands.multiagent.a2a import A2AServer
import uvicorn
from fastapi import FastAPI

from ddgs import DDGS
from ddgs.exceptions import RatelimitException, DDGSException


logging.basicConfig(level=logging.INFO)

# Use the complete runtime URL from environment variable, fallback to local
runtime_url = os.environ.get('AGENTCORE_RUNTIME_URL', 'http://127.0.0.1:9000/')

logging.info(f"�  Runtime URL: {runtime_url}")

@tool
def internet_search(keywords: str, region: str = "us-en", max_results: int | None = None) -> str:
    """Search the web to get updated information.
    Args:
        keywords (str): The search query keywords.
        region (str): The search region: wt-wt, us-en, uk-en, ru-ru, etc..
        max_results (int | None): The maximum number of results to return.
    Returns:
        List of dictionaries with search results.
    """
    try:
        results = DDGS().text(keywords, region=region, max_results=max_results)
        return results if results else "No results found."
    except RatelimitException:
        return "RatelimitException: Please try again after a short delay."
    except DDGSException as d:
        return f"DuckDuckGoSearchException: {d}"
    except Exception as e:
        return f"Exception: {e}"


system_prompt = """You are an AWS Blog Expert. 
You will use a internet search tool to get updates or news provided by AWS on:

AWS News Blog: https://aws.amazon.com/blogs/aws/
AWS Blogs for Machine Learning: https://aws.amazon.com/blogs/machine-learning/

Key capabilities:
- Search and retrieve information from Web using AWS oficial websites
- Don't get only homepage info, look for inner domains, like 
- Provide clear, accurate answers about question asked (most recent information)

Guidelines:
- Always prioritize official AWS pages as your source of truth
- Provide specific, actionable information when possible
- Include relevant links or references when helpful
- If you're unsure about something, clearly state your limitations
- Focus on being helpful, accurate, and concise in your responses
- Try to simplify/summarize answers to make it faster, small and objective

You have access to internet_search tools to help answer user questions effectively."""

agent = Agent(system_prompt=system_prompt, 
              tools=[internet_search],
              name="AWS Blog/News Agent",
              description="An agent to search on Web latest AWS Blogs and News.",
              callback_handler=None)

host, port = "0.0.0.0", 9000

# Pass runtime_url to http_url parameter AND use serve_at_root=True
a2a_server = A2AServer(
    agent=agent,
    http_url=runtime_url,
    serve_at_root=True  # Serves locally at root (/) regardless of remote URL path complexity
)

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "healthy"}

app.mount("/", a2a_server.to_fastapi_app())

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)