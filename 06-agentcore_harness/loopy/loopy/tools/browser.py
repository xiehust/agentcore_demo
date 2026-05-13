"""AgentCore Browser tool integration — wraps strands_tools AgentCoreBrowser."""

from __future__ import annotations

import asyncio
import json
import logging
from functools import cache
from typing import Any, Optional

from strands.types.tools import AgentTool, ToolGenerator, ToolUse
from strands_tools.browser import AgentCoreBrowser

from loopy.api_model.generated import HarnessAgentCoreBrowserConfig
from loopy.util.arn import region_from_arn, resource_id_from_arn

logger = logging.getLogger(__name__)


class _BrowserToolWrapper(AgentTool):
    """Wraps the strands_tools browser tool to coerce JSON-string browser_input.

    Some models serialize structured tool inputs as a JSON string rather than a
    nested object.  The underlying @tool validates against the BrowserInput
    Pydantic model, which rejects strings.  Parse the string to a dict before
    delegation so validation succeeds.
    """

    def __init__(self, inner: AgentTool) -> None:
        super().__init__()
        self._inner = inner

    @property
    def tool_name(self) -> str:
        return self._inner.tool_name

    @property
    def tool_spec(self):
        return self._inner.tool_spec

    @property
    def tool_type(self) -> str:
        return self._inner.tool_type

    def stream(self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any) -> ToolGenerator:
        tool_input = tool_use.get("input") or {}
        raw = tool_input.get("browser_input")
        if isinstance(raw, str):
            try:
                tool_input["browser_input"] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("browser_input was a non-JSON string; leaving as-is for validation error")
        return self._inner.stream(tool_use, invocation_state, **kwargs)


def _patch_event_loop(browser_instance: AgentCoreBrowser) -> None:
    """Workaround for https://github.com/strands-agents/tools/issues/453.

    Browser.__init__ sets the event loop on the init thread, but strands dispatches
    sync tools to a worker thread via asyncio.to_thread where no loop is set.
    This can be removed once the upstream fix is released.
    """
    loop = browser_instance._loop
    original = browser_instance._execute_async

    def _patched(coro):
        asyncio.set_event_loop(loop)
        return original(coro)

    browser_instance._execute_async = _patched


def create_browser_tool(region: str, config: Optional[HarnessAgentCoreBrowserConfig] = None) -> AgentCoreBrowser:
    """Create an AgentCoreBrowser instance from the payload config.

    If config.browserArn is set, the region and identifier are extracted from the ARN.
    Otherwise uses the provided region.
    """
    identifier = None

    if config and config.browserArn:
        region = region_from_arn(config.browserArn)
        identifier = resource_id_from_arn(config.browserArn)

    return _cached_browser(region, identifier)


@cache
def _cached_browser(region: str, identifier: Optional[str] = None) -> AgentCoreBrowser:
    """Cache browser instances by their hashable (region, identifier) key."""
    logger.info("Creating AgentCoreBrowser: region=%s, identifier=%s", region, identifier)

    kwargs: dict = {"region": region}
    if identifier:
        kwargs["identifier"] = identifier

    instance = AgentCoreBrowser(**kwargs)
    _patch_event_loop(instance)
    return instance
