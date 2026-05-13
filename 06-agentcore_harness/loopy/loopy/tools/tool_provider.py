"""Convert Loopy tool definitions from the invoke payload into Strands-compatible tools."""

from __future__ import annotations

import fnmatch
import logging
from typing import List

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.types.tools import AgentTool

from bedrock_agentcore.services.identity import IdentityClient
from loopy.abstract import LoopyContainerManager, LoopyToolProvider
from loopy.api_model.generated import AllowedTool, HarnessRemoteMcpConfig, HarnessTool, HarnessToolType
from loopy.tools.browser import _BrowserToolWrapper, create_browser_tool
from loopy.tools.code_interpreter import create_code_interpreter_tool
from loopy.tools.file_operations import create_file_operations_tool
from loopy.tools.gateway import create_gateway_mcp_client
from loopy.tools.inline_function import create_inline_function_tool
from loopy.tools.shell import create_shell_tool
from loopy.util.pydantic import reveal_secrets

logger = logging.getLogger(__name__)

_BEDROCK_TOOL_NAME_LIMIT = 64


def _shorten_gateway_tool_names(tools: list[AgentTool]) -> None:
    """Strip gateway prefix from tool names to stay under Bedrock's 64-char limit.

    Gateway tools have triple-nested naming: Strands prefixes our gateway name onto Gateway's
    own {targetName}___{mcpToolName}, producing {gatewayName}_{targetName}___{mcpToolName} which
    easily exceeds 64 chars. Remote MCP tools don't have this problem since MCP servers return
    short tool names without additional namespacing.

    Resets each tool's agent-facing name to the original MCP server name.
    Appends _2, _3, etc. if duplicates arise across gateways.
    """
    seen: dict[str, int] = {}
    for t in tools:
        name = t.mcp_tool.name
        seen[name] = seen.get(name, 0) + 1
        short_name = name if seen[name] == 1 else f"{name}_{seen[name]}"
        if len(short_name) > _BEDROCK_TOOL_NAME_LIMIT:
            raise ValueError(f"Gateway tool name '{name}' exceeds the maximum length. Combined target name and tool name must be at most 60 characters.")
        t._agent_tool_name = short_name


def _matches(pattern: str, tool_name: str, builtin_names: set[str]) -> bool:
    """Check if an allowedTools pattern matches a tool name.

    Supported patterns:
        "shell"              — exact builtin name match
        "file_*"             — glob against builtin names (matches file_operations, file_read)
        "@builtin"           — all builtin tools
        "@builtin/shell"     — specific builtin via namespace
        "@builtin/file_*"    — glob against builtins via namespace
        "@git"               — all tools from the 'git' MCP server (matches git/status, git/commit)
        "@git/git_status"    — specific MCP tool
        "@git/read_*"        — glob within a server (matches git/read_file, git/read_config)
        "@*-mcp/status"      — glob across servers (matches git-mcp/status, db-mcp/status)
    """
    if not pattern.startswith("@"):
        # Plain name or glob — matches builtins directly: "shell", "file_*"
        return fnmatch.fnmatchcase(tool_name, pattern)

    stripped = pattern[1:]  # remove leading @

    # @builtin or @builtin/pattern — special namespace for builtins
    if stripped == "builtin":
        return tool_name in builtin_names
    if stripped.startswith("builtin/"):
        return fnmatch.fnmatchcase(tool_name, stripped[len("builtin/"):])

    # @server (no slash) — all tools from that server: "@git" matches "git/*"
    if "/" not in stripped:
        return tool_name.startswith(stripped + "/")

    # @server/tool or @server/glob — glob against tool_name: "@git/read_*" matches "git/read_file"
    return fnmatch.fnmatchcase(tool_name, stripped)


def _filter_allowed(
    tools: list[AgentTool], allowed_tools: list[AllowedTool], builtin_names: set[str] = frozenset()
) -> list[AgentTool]:
    """Filter tools to only those permitted by the allowedTools list.

    "*" permits all tools. An empty list permits none.
    """
    patterns = {a.root for a in allowed_tools}
    if "*" in patterns:
        return tools

    filtered = [t for t in tools if any(_matches(p, t.tool_name, builtin_names) for p in patterns)]
    unmatched = {p for p in patterns if not any(_matches(p, t.tool_name, builtin_names) for t in tools)}
    if unmatched:
        logger.warning("allowedTools entries matched no resolved tools: %s", unmatched)
    logger.info("allowedTools filter: %d/%d tools permitted", len(filtered), len(tools))
    return filtered


def create_remote_mcp_client(name: str, config: HarnessRemoteMcpConfig) -> MCPClient:
    """Create a Strands MCPClient for a remote MCP server using streamable HTTP."""
    url = reveal_secrets(config.url)
    headers = reveal_secrets(config.headers.root) if config.headers else None

    return MCPClient(
        lambda _url=url, _headers=headers: streamablehttp_client(url=_url, headers=_headers),
        prefix=name,
    )


class ToolProvider(LoopyToolProvider):
    def __init__(self, container_manager: LoopyContainerManager, identity_client: IdentityClient, region: str = "us-west-2") -> None:
        self._container_manager = container_manager
        self._region = region
        self._identity_client = identity_client

    async def resolve_tools(self, tools: List[HarnessTool], allowed_tools: List[AllowedTool]) -> List[AgentTool]:
        resolved_servers: list[tuple[str, MCPClient]] = []
        gateway_servers: list[tuple[str, MCPClient]] = []
        inline_tools: list[AgentTool] = []
        browser_tools: list[AgentTool] = []
        code_interpreter_tools: list[AgentTool] = []

        builtin_tools: list[AgentTool] = [
            create_shell_tool(self._container_manager),
            create_file_operations_tool(self._container_manager),
        ]
        builtin_names = {t.tool_name for t in builtin_tools}

        # Validate: no duplicate tool names, no multiple browser/code interpreter instances
        seen_names: set[str] = set(builtin_names)
        browser_count = 0
        code_interpreter_count = 0
        for tool in tools:
            name = tool.name
            if name and name in seen_names:
                raise ValueError(f"Duplicate tool name: '{name}'. Each tool must have a unique name.")
            if name:
                seen_names.add(name)
            if tool.type == HarnessToolType.agentcore_browser:
                browser_count += 1
                if browser_count > 1:
                    raise ValueError("Multiple browser tools are not supported. Provide at most one agentcore_browser tool.")
            elif tool.type == HarnessToolType.agentcore_code_interpreter:
                code_interpreter_count += 1
                if code_interpreter_count > 1:
                    raise ValueError("Multiple code interpreter tools are not supported. Provide at most one agentcore_code_interpreter tool.")

        for tool in tools:
            try:
                match tool.type:
                    case HarnessToolType.remote_mcp:
                        resolved_servers.append((tool.name, create_remote_mcp_client(tool.name, tool.config.root.remoteMcp)))
                    case HarnessToolType.agentcore_gateway:
                        gateway_servers.append((tool.name, create_gateway_mcp_client(tool.name, tool.config.root.agentCoreGateway, identity_client=self._identity_client)))
                    case HarnessToolType.agentcore_browser:
                        browser_config = tool.config.root.agentCoreBrowser if tool.config else None
                        browser_instance = create_browser_tool(self._region, browser_config)
                        browser_tools.append(_BrowserToolWrapper(browser_instance.browser))
                    case HarnessToolType.agentcore_code_interpreter:
                        ci_config = tool.config.root.agentCoreCodeInterpreter if tool.config else None
                        ci_instance = create_code_interpreter_tool(self._region, ci_config)
                        code_interpreter_tools.append(ci_instance.code_interpreter)
                    case HarnessToolType.inline_function:
                        inline_tools.append(
                            create_inline_function_tool(tool.name, tool.config.root.inlineFunction)
                        )
            except Exception as e:
                raise RuntimeError(f"Failed to create tool '{tool.name}' (type={tool.type}): {e}") from e

        resolved_tools: list[AgentTool] = list(builtin_tools)
        resolved_tools.extend(inline_tools)
        resolved_tools.extend(browser_tools)
        resolved_tools.extend(code_interpreter_tools)
        for name, server in resolved_servers:
            try:
                resolved_tools.extend(await server.load_tools())
            except Exception as e:
                raise RuntimeError(f"Failed to load tool '{name}': {e}") from e

        gateway_tools: list[AgentTool] = []
        for name, server in gateway_servers:
            try:
                gateway_tools.extend(await server.load_tools())
            except Exception as e:
                raise RuntimeError(f"Failed to load tool '{name}': {e}") from e

        filtered_gateway = _filter_allowed(gateway_tools, allowed_tools, builtin_names)
        _shorten_gateway_tool_names(filtered_gateway)

        filtered_rest = _filter_allowed(resolved_tools, allowed_tools, builtin_names)
        return filtered_rest + filtered_gateway
