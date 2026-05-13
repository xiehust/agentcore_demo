"""AgentCore Code Interpreter tool integration — wraps strands_tools AgentCoreCodeInterpreter."""

from __future__ import annotations

import logging
from functools import cache
from typing import Optional

from strands_tools.code_interpreter import AgentCoreCodeInterpreter

from loopy.api_model.generated import HarnessAgentCoreCodeInterpreterConfig
from loopy.util.arn import region_from_arn, resource_id_from_arn

logger = logging.getLogger(__name__)


def create_code_interpreter_tool(
    region: str, config: Optional[HarnessAgentCoreCodeInterpreterConfig] = None
) -> AgentCoreCodeInterpreter:
    """Create an AgentCoreCodeInterpreter instance from the payload config.

    If config.codeInterpreterArn is set, the identifier is extracted from the ARN.
    Otherwise uses the provided region with the default identifier.
    """
    identifier = None

    if config and config.codeInterpreterArn:
        region = region_from_arn(config.codeInterpreterArn)
        # The StartCodeInterpreterSession API expects the short identifier, not the full ARN.
        # See https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_StartCodeInterpreterSession.html
        identifier = resource_id_from_arn(config.codeInterpreterArn)

    return _cached_code_interpreter(region, identifier)


@cache
def _cached_code_interpreter(region: str, identifier: Optional[str] = None) -> AgentCoreCodeInterpreter:
    """Cache code interpreter instances by their hashable (region, identifier) key."""
    logger.info("Creating AgentCoreCodeInterpreter: region=%s, identifier=%s", region, identifier)

    kwargs: dict = {"region": region}
    if identifier:
        kwargs["identifier"] = identifier

    return AgentCoreCodeInterpreter(**kwargs)
