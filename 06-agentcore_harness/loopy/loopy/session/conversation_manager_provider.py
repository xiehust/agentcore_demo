"""Conversation manager provider for Loopy runtime.

Reads truncation strategy from environment variables (set by CP WorkflowLambda)
and returns the appropriate Strands ConversationManager.
"""

import logging
from typing import Any, Optional

from strands.agent.conversation_manager.conversation_manager import ConversationManager
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from strands.agent.conversation_manager.sliding_window_conversation_manager import SlidingWindowConversationManager
from strands.agent.conversation_manager.summarizing_conversation_manager import SummarizingConversationManager

from loopy.abstract import LoopyConversationManagerProvider
from loopy.util.constants import TruncationStrategy

logger = logging.getLogger(__name__)


class ConversationManagerProvider(LoopyConversationManagerProvider):
    """Creates the appropriate ConversationManager based on truncation config from env vars."""

    def __init__(self, strategy: Optional[TruncationStrategy] = None, config: Optional[dict[str, Any]] = None) -> None:
        self._strategy = strategy
        self._config = config or {}

    def resolve_conversation_manager(self) -> ConversationManager:
        match self._strategy:
            case TruncationStrategy.SLIDING_WINDOW:
                logger.info("strategy=<sliding_window> config=<%s>", self._config)
                return SlidingWindowConversationManager(**self._config, per_turn=True)
            case TruncationStrategy.SUMMARIZATION:
                logger.info("strategy=<summarization> config=<%s>", self._config)
                return SummarizingConversationManager(**self._config)
            case TruncationStrategy.NONE | None:
                return NullConversationManager()
