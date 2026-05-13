import abc
from collections.abc import AsyncIterator
from typing import Any, Awaitable, List, Optional

from strands.agent.conversation_manager.conversation_manager import ConversationManager
from strands.models.model import Model
from strands.session.session_manager import SessionManager
from strands.types.tools import AgentTool

from loopy.api_model.generated import AllowedTool, HarnessModelConfiguration, HarnessTool
from loopy.api_model.request import ContainerRequest, Credentials
from loopy.container.types import StreamChunk


class LoopyModelProvider(abc.ABC):
    """Resolves a HarnessModelConfiguration from the invoke payload into a Strands Model."""

    @abc.abstractmethod
    def resolve_model(self, model_config: HarnessModelConfiguration) -> Model: ...


class LoopyToolProvider(abc.ABC):
    """Resolves tool definitions from the invoke payload into Strands Tools."""

    @abc.abstractmethod
    def resolve_tools(self, tools: List[HarnessTool], allowed_tools: List[AllowedTool]) -> Awaitable[List[AgentTool]]: ...


class LoopySessionManagerProvider(abc.ABC):
    """Creates a SessionManager for persisting messages across invocations."""

    @abc.abstractmethod
    def resolve_session_manager(self, session_id: str) -> SessionManager: ...


class LoopyConversationManagerProvider(abc.ABC):
    """Creates a ConversationManager for truncating conversation history."""

    @abc.abstractmethod
    def resolve_conversation_manager(self) -> ConversationManager: ...


class FileIO(abc.ABC):
    """Abstraction for file I/O — local Python or CinC shell-based."""

    @abc.abstractmethod
    def read(self, path: str) -> str: ...

    @abc.abstractmethod
    def write(self, path: str, content: str) -> None: ...

    @abc.abstractmethod
    def exists(self, path: str) -> bool: ...

    @abc.abstractmethod
    def is_dir(self, path: str) -> bool: ...

    @abc.abstractmethod
    def listdir(self, path: str) -> str: ...

    @abc.abstractmethod
    def mkdir_parents(self, path: str) -> None: ...


class LoopyContainerManager(abc.ABC):
    """Abstraction for running commands and file operations — either locally or in a CinC customer container."""

    @abc.abstractmethod
    async def ensure_started(self, credentials: Optional[Credentials] = None) -> None: ...

    @abc.abstractmethod
    def run(self, command: str, timeout: int = 300) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def run_async(self, command: str, timeout: int = 300) -> AsyncIterator[StreamChunk]: ...

    @property
    @abc.abstractmethod
    def is_customer_container(self) -> bool: ...

    @property
    @abc.abstractmethod
    def file_io(self) -> FileIO: ...


class LoopyHandler(abc.ABC):
    """Handles a ContainerRequest — either invoke (agent loop) or execute (shell command)."""

    @abc.abstractmethod
    def handle(self, request: ContainerRequest, context: Any = None) -> AsyncIterator[dict[str, Any]]: ...
