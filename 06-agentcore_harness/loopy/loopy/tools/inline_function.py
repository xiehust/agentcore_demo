"""Inline function tool — stops the agent loop so the tool call streams back to the caller."""

from typing import Any

from strands.tools.tools import PythonAgentTool
from strands.types.tools import AgentTool, ToolResult, ToolUse

from loopy.api_model.generated import HarnessInlineFunctionConfig
from loopy.util.pydantic import reveal_secrets


def create_inline_function_tool(name: str, config: HarnessInlineFunctionConfig) -> AgentTool:
    """Create a Strands tool that stops the event loop when called.

    The tool call events (including the LLM-generated input) are streamed to the
    caller by the invoke handler. This tool just needs to halt the loop.
    """
    tool_spec = {
        "name": name,
        "description": reveal_secrets(config.description),
        "inputSchema": {"json": config.inputSchema if isinstance(config.inputSchema, dict) else {}},
    }

    def handler(tool: ToolUse, **kwargs: Any) -> ToolResult:
        kwargs.get("request_state", {})["stop_event_loop"] = True
        return {"toolUseId": tool["toolUseId"], "status": "success", "content": [{"text": ""}]}

    handler.__name__ = name
    return PythonAgentTool(tool_name=name, tool_spec=tool_spec, tool_func=handler)
