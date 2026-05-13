import logging
from typing import Any, AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext, RequestContext

from loopy.abstract import LoopyHandler
from loopy.api_model.generated import InvokeHarnessStreamOutput9 as RuntimeClientErrorEvent, RuntimeClientError
from loopy.api_model.request import ContainerRequest, Operation
from loopy.config import EnvConfig
from loopy.container.ctr_container_manager import CtrContainerManager, create_ecr_client
from loopy.container.local_container_manager import LocalContainerManager
from loopy.handler.invoke import InvokeHandler
from loopy.handler.invoke_agent_runtime_command import InvokeAgentRuntimeCommandHandler
from loopy.model.model_provider import ModelProvider
from loopy.session.conversation_manager_provider import ConversationManagerProvider
from loopy.session.session_manager import SessionProvider
from loopy.tools.tool_provider import ToolProvider
from loopy.util.identity import create_identity_client
from loopy.util.constants import WORKLOAD_ACCESS_TOKEN_HEADER

class RequestContextFilter(logging.Filter):
    """Injects requestId and sessionId from BedrockAgentCoreApp context into every log record."""

    def filter(self, record):
        record.requestId = RequestContext.get_request_id() or "-"
        record.sessionId = RequestContext.get_session_id() or "-"
        return True


def create_app(invoke_handler: LoopyHandler, invoke_agent_runtime_command_handler: LoopyHandler) -> BedrockAgentCoreApp:
    app = BedrockAgentCoreApp()

    @app.entrypoint
    async def invoke(payload: dict[str, Any], context: RequestContext) -> AsyncIterator[dict[str, Any]]:
        # The SDK expects header "WorkloadAccessToken" but the platform sends
        # "x-amzn-bedrock-agentcore-workload-access-token". Bridge the gap so
        # BedrockAgentCoreContext has the token available for downstream code.
        headers = dict(context.request.headers) if context.request else {}
        token = headers.get(WORKLOAD_ACCESS_TOKEN_HEADER)
        if token and not BedrockAgentCoreContext.get_workload_access_token():
            BedrockAgentCoreContext.set_workload_access_token(token)

        request = ContainerRequest(**payload)
        match request.operation:
            case Operation.INVOKE:
                handler = invoke_handler
            case Operation.INVOKE_AGENT_RUNTIME_COMMAND:
                handler = invoke_agent_runtime_command_handler
            case _:
                raise RuntimeError("Unknown operation")
        try:
            async for event in handler.handle(request, context):
                yield event
        except Exception as e:
            app.logger.exception("Error in sync streaming")
            event = RuntimeClientErrorEvent(runtimeClientError=RuntimeClientError(message=str(e)))
            yield {"event": event.model_dump(exclude_none=True)}
    return app


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addFilter(RequestContextFilter())

    config = EnvConfig.from_env()

    container_manager = CtrContainerManager(config.container_uri, create_ecr_client(config.container_uri), filesystem_mount_paths=config.filesystem_mount_paths) if config.container_uri else LocalContainerManager()
    identity_client = create_identity_client(config.region, config.stage)
    
    tool_provider = ToolProvider(container_manager, region=config.region, identity_client=identity_client)
    session_provider = SessionProvider(memory_arn=config.memory_arn, actor_id=config.memory_actor_id)
    conversation_manager_provider = ConversationManagerProvider(strategy=config.truncation_strategy, config=config.truncation_config)

    invoke_handler = InvokeHandler(
        ModelProvider(identity_client, region=config.region), tool_provider, session_provider, container_manager, conversation_manager_provider
    )
    command_handler = InvokeAgentRuntimeCommandHandler(container_manager)

    app = create_app(invoke_handler, command_handler)
    app.run(host="0.0.0.0", log_level="info")


if __name__ == "__main__":
    main()
