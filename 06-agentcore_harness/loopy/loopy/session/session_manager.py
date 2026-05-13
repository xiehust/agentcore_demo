"""Session management for Loopy runtime.

Provides two SessionManager implementations:
- LoopyFileSessionManager: persists messages to the local filesystem (default)
- LoopyAgentCoreMemorySessionManager: delegates to the SDK's AgentCoreMemorySessionManager,
  overriding initialize() to skip the conversation_manager __name__ check

The file session manager persists the post-trimming snapshot directly. The memory session manager
delegates to the SDK for message writing, agent state storage, and conversation manager state
persistence — but restores conversation manager state manually to avoid ValueError when customers
change truncation strategy via UpdateLoopyAgent.
"""

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from bedrock_agentcore.memory.client import MemoryClient
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands.hooks import MessageAddedEvent
from strands.hooks.registry import HookRegistry
from strands.session.session_manager import SessionManager
from strands.types.content import Message
from strands.types.exceptions import SessionException
from strands.types.session import SessionAgent, SessionMessage

from loopy.abstract import LoopySessionManagerProvider
from loopy.api_model.request import HarnessAgentCoreMemoryConfiguration as LoopyMemoryConfig
from loopy.util.constants import DEFAULT_ACTOR_ID
from loopy.util.arn import region_from_arn, resource_id_from_arn

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

_STORAGE_DIR = "/tmp/loopy/sessions"
_MESSAGES_FILE = "messages.json"


def _fix_broken_tool_use(messages: list[Message]) -> list[Message]:
    """Fix orphaned toolUse/toolResult pairs in message history.

    Handles two cases that cause Bedrock ConverseStream to reject the history:
    1. Duplicate toolResult for the same toolUseId (e.g. "Tool was interrupted" + actual result)
    2. Orphaned toolResult at the start (from truncation removing the preceding toolUse)
    """
    if not messages:
        return messages

    # Remove orphaned toolResult at the start (no preceding toolUse)
    if messages[0]["role"] == "user" and all("toolResult" in c for c in messages[0]["content"]):
        logger.warning("Removing orphaned toolResult at start of message history")
        messages = messages[1:]

    # Deduplicate toolResults — keep only the last result per toolUseId
    for i, message in enumerate(messages):
        if message["role"] != "user":
            continue
        seen_tool_ids: set[str] = set()
        deduped: list = []
        for content in reversed(message["content"]):
            if "toolResult" in content:
                tid = content["toolResult"]["toolUseId"]
                if tid in seen_tool_ids:
                    logger.warning("Removing duplicate toolResult for toolUseId=%s", tid)
                    continue
                seen_tool_ids.add(tid)
            deduped.append(content)
        deduped.reverse()
        if len(deduped) != len(message["content"]):
            messages[i] = {**message, "content": deduped}

    return messages


class LoopyFileSessionManager(SessionManager):
    """Persists only messages across invocations within a microVM session."""

    def __init__(self, storage_dir: str = _STORAGE_DIR) -> None:
        self._storage_dir = storage_dir
        self._messages_path = os.path.join(storage_dir, _MESSAGES_FILE)
        os.makedirs(storage_dir, exist_ok=True)

    def _read_messages(self) -> list[Message]:
        """Load messages from disk, decoding any base64-encoded bytes values."""
        if not os.path.exists(self._messages_path):
            return []
        with open(self._messages_path, "r") as f:
            raw = json.load(f)
        return [SessionMessage.from_dict(m).to_message() for m in raw]

    def _write_messages(self, messages: list[Message]) -> None:
        """Write messages to disk atomically, encoding bytes values for JSON safety."""
        session_messages = [SessionMessage.from_message(m, i).to_dict() for i, m in enumerate(messages)]
        tmp = f"{self._messages_path}.tmp"
        with open(tmp, "w") as f:
            json.dump(session_messages, f)
        os.replace(tmp, self._messages_path)

    def initialize(self, agent: "Agent", **kwargs: Any) -> None:
        """Restore messages from previous invocations into the new Agent."""
        messages = self._read_messages()
        if messages:
            logger.info("message_count=<%d> | restoring messages from previous invocation", len(messages))
            agent.messages = _fix_broken_tool_use(messages)

    def append_message(self, message: Message, agent: "Agent", **kwargs: Any) -> None:
        """No-op — sync_agent() is called immediately after and writes the authoritative snapshot."""
        pass

    def sync_agent(self, agent: "Agent", **kwargs: Any) -> None:
        """Write the post-trimming message snapshot to disk."""
        self._write_messages(list(agent.messages))

    def redact_latest_message(self, redact_message: Message, agent: "Agent", **kwargs: Any) -> None:
        """Replace the most recent message (e.g. guardrail redaction)."""
        messages = self._read_messages()
        if not messages:
            raise SessionException("No message to redact.")
        messages[-1] = redact_message
        self._write_messages(messages)


class LoopyAgentCoreMemorySessionManager(SessionManager):
    """Delegates to the SDK's AgentCoreMemorySessionManager for message and state persistence.

    Overrides initialize() to restore conversation_manager state without calling
    restore_from_session(), which raises ValueError when the strategy class name changes
    (e.g. customer switches from SlidingWindow to Summarizing via UpdateLoopyAgent).
    """

    def __init__(self, memory_id: str, session_id: str, actor_id: str, region_name: str, retrieval_config: Optional[dict] = None) -> None:
        retrieval_cfg = None
        if retrieval_config:
            retrieval_cfg = {ns: RetrievalConfig(**cfg) for ns, cfg in retrieval_config.items()}
        config = AgentCoreMemoryConfig(
            memory_id=memory_id, session_id=session_id, actor_id=actor_id, retrieval_config=retrieval_cfg
        )
        self._delegate = AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=region_name)

    @classmethod
    def from_delegate(cls, delegate: AgentCoreMemorySessionManager) -> "LoopyAgentCoreMemorySessionManager":
        """Create from a pre-built delegate. Used in tests to inject a mock memory client."""
        instance = cls.__new__(cls)
        instance._delegate = delegate
        return instance

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register our hooks + the delegate's LTM retrieval hook.

        We skip the delegate's full register_hooks() because it chains through
        RepositorySessionManager → SessionManager, which registers the delegate's
        initialize/append_message/sync_agent as additional callbacks. The delegate's
        initialize calls restore_from_session() with the __name__ check we're avoiding.

        But we do need the delegate's retrieve_customer_context hook for LTM retrieval.
        """
        super().register_hooks(registry, **kwargs)
        if self._delegate.config.retrieval_config:
            registry.add_callback(MessageAddedEvent, lambda event: self._delegate.retrieve_customer_context(event))

    def initialize(self, agent: "Agent", **kwargs: Any) -> None:
        """Restore agent state from Memory, skipping restore_from_session() __name__ check."""
        session_id = self._delegate.session_id
        agent_id = agent.agent_id

        # NOTE: _latest_agent_message and _is_new_session are private fields on the delegate.
        # We access them because there's no public API to derive this info. Acceptable since we
        # control the Strands version we consume and work closely with the Strands team.

        # Track agent for the delegate's internal bookkeeping
        self._delegate._latest_agent_message[agent_id] = None

        # Skip read_agent for new sessions
        if self._delegate._is_new_session:
            session_agent = None
        else:
            session_agent = self._delegate.read_agent(session_id, agent_id)

        if session_agent is None:
            logger.info("agent_id=<%s> | session_id=<%s> | creating agent in Memory", agent_id, session_id)
            session_agent = SessionAgent.from_agent(agent)
            self._delegate.create_agent(session_id, session_agent)
            sm = None
            for i, message in enumerate(agent.messages):
                sm = SessionMessage.from_message(message, i)
                self._delegate.create_message(session_id, agent_id, sm)
            self._delegate._latest_agent_message[agent_id] = sm
        else:
            logger.info("agent_id=<%s> | session_id=<%s> | restoring agent from Memory", agent_id, session_id)

            session_agent.initialize_internal_state(agent)

            # Restore conversation manager state manually — skip restore_from_session() to avoid __name__ check
            cm_state = session_agent.conversation_manager_state or {}
            removed_count = cm_state.get("removed_message_count", 0)
            agent.conversation_manager.removed_message_count = removed_count

            # For SummarizingConversationManager, restore the summary message
            summary_message = cm_state.get("summary_message")
            if summary_message and hasattr(agent.conversation_manager, "_summary_message"):
                agent.conversation_manager._summary_message = summary_message
                prepend_messages = [summary_message]
            else:
                prepend_messages = []

            # Load messages with offset (skip messages already removed by conversation manager)
            session_messages = self._delegate.list_messages(
                session_id=session_id,
                agent_id=agent_id,
                offset=removed_count,
            )
            if session_messages:
                self._delegate._latest_agent_message[agent_id] = session_messages[-1]

            if getattr(agent.model, "stateful", False):
                logger.info("agent_id=<%s> | session_id=<%s> | skipping message restore for server-managed conversation", agent_id, session_id)
            else:
                agent.messages = _fix_broken_tool_use(
                    prepend_messages + [sm.to_message() for sm in session_messages]
                )
                logger.info("message_count=<%d> | restored messages from Memory", len(agent.messages))

        self._delegate._is_new_session = False

    def append_message(self, message: Message, agent: "Agent", **kwargs: Any) -> None:
        self._delegate.append_message(message, agent, **kwargs)

    def sync_agent(self, agent: "Agent", **kwargs: Any) -> None:
        self._delegate.sync_agent(agent, **kwargs)

    def redact_latest_message(self, redact_message: Message, agent: "Agent", **kwargs: Any) -> None:
        self._delegate.redact_latest_message(redact_message, agent, **kwargs)

class SessionProvider(LoopySessionManagerProvider):
    """Creates the appropriate SessionManager based on whether a memory ARN is configured."""

    def __init__(
        self,
        memory_arn: Optional[str] = None,
        actor_id: Optional[str] = None,
    ) -> None:
        self._memory_arn = memory_arn
        self._actor_id = actor_id or DEFAULT_ACTOR_ID
        self._memory_id = resource_id_from_arn(memory_arn) if memory_arn else None
        self._region = region_from_arn(memory_arn) if memory_arn else None
        self._retrieval_config: Optional[dict] = None

    @classmethod
    def from_memory_config(cls, memory_config: LoopyMemoryConfig) -> "SessionProvider":
        """Create from a MemoryConfig received in the container request payload."""
        provider = cls(memory_arn=memory_config.arn, actor_id=memory_config.actorId)
        if memory_config.retrievalConfig:
            provider._retrieval_config = {
                namespace: rc.model_dump(exclude_none=True)
                for namespace, rc in memory_config.retrievalConfig.items()
            }
        return provider

    def resolve_session_manager(self, session_id: str) -> SessionManager:
        if self._memory_arn:
            return LoopyAgentCoreMemorySessionManager(
                memory_id=self._memory_id,
                session_id=session_id,
                actor_id=self._actor_id,
                region_name=self._region,
                retrieval_config=self._retrieval_config,
            )
        return LoopyFileSessionManager()
