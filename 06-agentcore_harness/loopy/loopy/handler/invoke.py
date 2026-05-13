import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from strands import Agent, AgentSkills
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import EventLoopException

from loopy.abstract import (
    LoopyContainerManager,
    LoopyConversationManagerProvider,
    LoopyHandler,
    LoopyModelProvider,
    LoopySessionManagerProvider,
    LoopyToolProvider,
)
from loopy.api_model.generated import (
    HarnessContentBlockStart,
    HarnessContentBlockStart2,
    HarnessContentBlockStartEvent,
    HarnessContentBlockStopEvent,
    InvokeHarnessStreamOutput1,
    InvokeHarnessStreamOutput2,
    InvokeHarnessStreamOutput4,
    InvokeHarnessStreamOutput5,
    HarnessConversationRole,
    HarnessMessageStartEvent,
    HarnessMessageStopEvent,
    HarnessStopReason,
    HarnessTool,
    HarnessToolResultBlockStart,
    HarnessToolType,
)
from loopy.api_model.request import ContainerRequest
from loopy.hooks.execution_limits import ExecutionLimitExceeded, ExecutionLimitsHook
from loopy.session.session_manager import SessionProvider
from loopy.util.pydantic import reveal_secrets

logger = logging.getLogger(__name__)


class InvokeHandler(LoopyHandler):
    def __init__(
        self,
        model_provider: LoopyModelProvider,
        tool_provder: LoopyToolProvider,
        session_manager_provider: LoopySessionManagerProvider,
        container_manager: LoopyContainerManager,
        conversation_manager_provider: LoopyConversationManagerProvider,
    ) -> None:
        super().__init__()
        self.model_provider: LoopyModelProvider = model_provider
        self.tool_provder: LoopyToolProvider = tool_provder
        self.session_manager_provider: LoopySessionManagerProvider = session_manager_provider
        self._container_manager: LoopyContainerManager = container_manager
        self._conversation_manager_provider: LoopyConversationManagerProvider = conversation_manager_provider

    async def handle(self, request: ContainerRequest, context: Any = None) -> AsyncIterator[dict[str, Any]]:
        payload = request.invokePayload
        if not payload or payload.model is None or payload.systemPrompt is None or payload.tools is None or payload.allowedTools is None or payload.messages is None:
            raise ValueError("invokePayload is missing required fields")

        session_id = context.session_id if context else None
        if not session_id:
            raise ValueError("runtimeSessionId is required")

        await self._container_manager.ensure_started(request.credentials)

        # Collect inline_function tool names so we can intercept their tool calls
        inline_function_names = _get_inline_function_names(payload.tools)

        model = self.model_provider.resolve_model(payload.model)
        tools = await self.tool_provder.resolve_tools(payload.tools, payload.allowedTools)
        system_prompt = reveal_secrets([s.model_dump(exclude_none=True) for s in payload.systemPrompt])

        # Prefer memoryConfig from payload (per-invocation), fall back to env var config
        memory_config = request.memoryConfig.agentCoreMemoryConfiguration if request.memoryConfig else None
        if memory_config and memory_config.arn:
            session_provider = SessionProvider.from_memory_config(memory_config)
        else:
            session_provider = self.session_manager_provider
        session_manager = session_provider.resolve_session_manager(session_id)
        conversation_manager = self._conversation_manager_provider.resolve_conversation_manager()

        plugins = []
        if payload.skills:
            plugins.append(AgentSkills(skills=[s.root for s in payload.skills]))

        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tool_executor=SequentialToolExecutor(),
            tools=tools,
            session_manager=session_manager,
            conversation_manager=conversation_manager,
            callback_handler=None,
            plugins=plugins or None,
            hooks=[
                ExecutionLimitsHook(
                    max_iterations=int(payload.maxIterations) if payload.maxIterations is not None else None,
                    max_tokens=int(payload.maxTokens) if payload.maxTokens is not None else None,
                    timeout_seconds=payload.timeoutSeconds,
                )
            ],
        )

        # Start a background watchdog that cancels the agent via Strands' cancel_signal
        # when the timeout expires. The cancel_signal is checked between every streaming
        # chunk, so the agent stops cleanly with proper lifecycle events (sync_agent, etc.).
        # The existing BeforeModelCallEvent check in ExecutionLimitsHook remains as a
        # fast-path that avoids starting a new model call when already over budget.
        timeout_fired = False
        watchdog_task = None
        if payload.timeoutSeconds is not None:

            async def _timeout_watchdog():
                nonlocal timeout_fired
                await asyncio.sleep(payload.timeoutSeconds)
                timeout_fired = True
                agent.cancel()

            watchdog_task = asyncio.create_task(_timeout_watchdog())

        try:
            # Strands stream_async yields two kinds of useful events:
            #   {"event": chunk} - model stream (ConverseStream-shaped, pass through as-is)
            #   {"message": msg} - complete messages (tool results, assistant turns)
            # Skipped: callback handler duplicates (non-serializable), lifecycle signals, and agent results.
            hit_inline_function = False
            async for event in agent.stream_async(
                reveal_secrets([m.model_dump(exclude_none=True) for m in payload.messages])
            ):
                if not isinstance(event, dict):
                    continue
                if "event" in event:
                    # Drop contentBlockStart with empty start (e.g. OpenAI text blocks)
                    cbs = event["event"].get("contentBlockStart")
                    if cbs is not None and not cbs.get("start"):
                        continue
                    # Detect if the model is calling an inline_function tool
                    if not hit_inline_function:
                        hit_inline_function = _is_inline_function_call(event["event"], inline_function_names)
                    yield event
                    # After the messageStop for an inline_function tool call, stop streaming
                    if hit_inline_function and "messageStop" in event.get("event", {}):
                        return
                elif "message" in event:
                    msg = event["message"]
                    # Suppress tool result events for inline_function tools (the loop already stopped)
                    if hit_inline_function:
                        continue
                    # Only transform tool result messages — assistant messages are already
                    # streamed via "event" chunks and would be duplicated.
                    if any("toolResult" in block for block in msg.get("content", [])):
                        for transformed in _tool_result_to_events(msg):
                            yield transformed

            # If the watchdog fired, Strands ended cleanly via cancel_signal but our handler
            # needs to emit a timeout_exceeded messageStop (Strands' "cancelled" stop_reason
            # flows through AgentResult, which we don't forward to the customer).
            if timeout_fired:
                message_stop = InvokeHarnessStreamOutput5(
                    messageStop=HarnessMessageStopEvent(stopReason=HarnessStopReason.timeout_exceeded)
                )
                yield {"event": message_stop.model_dump(exclude_none=True)}
        except EventLoopException as e:
            if isinstance(e.original_exception, ExecutionLimitExceeded):
                message_stop = InvokeHarnessStreamOutput5(
                    messageStop=HarnessMessageStopEvent(stopReason=e.original_exception.stop_reason)
                )
                yield {"event": message_stop.model_dump(exclude_none=True)}
                return
            raise
        finally:
            # Clean up the watchdog task if the agent finished before the timeout
            if watchdog_task is not None:
                watchdog_task.cancel()
                # Await the cancelled task so Python doesn't log a noisy warning
                # about an unhandled exception in a fire-and-forget task.
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass


def _tool_result_to_events(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Transform a Strands ToolResultMessageEvent into ConverseStream-shaped events.

    Strands yields tool results as:
        {"role": "user", "content": [{"toolResult": {"toolUseId": "...", "status": "...", "content": [...]}}]}

    InvokeHarnessStreamOutput expects messageStart → contentBlockStart/Delta/Stop → messageStop.
    Uses generated Pydantic models for messageStart/messageStop (no SecretStr/float issues).
    Uses plain dicts for contentBlock events (generated models have SecretStr text fields and float indices).
    """

    message_start = InvokeHarnessStreamOutput1(messageStart=HarnessMessageStartEvent(role=HarnessConversationRole.user))
    message_stop = InvokeHarnessStreamOutput5(messageStop=HarnessMessageStopEvent(stopReason=HarnessStopReason.tool_result))

    events: list[dict[str, Any]] = [{"event": message_start.model_dump(exclude_none=True)}]
    for i, block in enumerate(message.get("content", [])):
        if "toolResult" not in block:
            continue
        tr = block["toolResult"]
        start = InvokeHarnessStreamOutput2(
            contentBlockStart=HarnessContentBlockStartEvent(
                contentBlockIndex=i,
                start=HarnessContentBlockStart(
                    root=HarnessContentBlockStart2(
                        toolResult=HarnessToolResultBlockStart(toolUseId=tr["toolUseId"], status=tr.get("status")),
                    )
                ),
            )
        )
        events.append({"event": start.model_dump(exclude_none=True)})
        if tr.get("content"):
            for chunk in _chunk_tool_result_content(tr["content"]):
                events.append(
                    {
                        "event": {
                            "contentBlockDelta": {
                                "contentBlockIndex": i,
                                "delta": {"toolResult": chunk},
                            }
                        }
                    }
                )
        stop = InvokeHarnessStreamOutput4(contentBlockStop=HarnessContentBlockStopEvent(contentBlockIndex=i))
        events.append({"event": stop.model_dump(exclude_none=True)})
    events.append({"event": message_stop.model_dump(exclude_none=True)})
    return events


# Keep each SSE event well under the ~8KB ByteBuffer boundary observed in Runtime.
_MAX_CHUNK_CHARS = 1024


def _chunk_tool_result_content(content: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split tool result content blocks so each delta stays small.

    Text blocks larger than _MAX_CHUNK_CHARS are split across multiple deltas.
    Non-text blocks (json) are emitted as-is.
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for block in content:
        if "text" not in block or len(block["text"]) <= _MAX_CHUNK_CHARS:
            current.append(block)
            continue
        # Flush anything accumulated before this large text block
        if current:
            chunks.append(current)
            current = []
        # Split the large text
        text = block["text"]
        for offset in range(0, len(text), _MAX_CHUNK_CHARS):
            chunks.append([{"text": text[offset : offset + _MAX_CHUNK_CHARS]}])

    if current:
        chunks.append(current)
    return chunks or [content]


def _get_inline_function_names(tools: list[HarnessTool]) -> set[str]:
    """Return the set of tool names that are inline_function type."""
    return {t.name for t in tools if t.type == HarnessToolType.inline_function}


def _is_inline_function_call(event: dict[str, Any], inline_names: set[str]) -> bool:
    """Check if a stream event is a contentBlockStart for an inline_function tool."""
    if not inline_names:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in inline_names
