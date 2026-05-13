"""InvokeAgentRuntimeCommand handler — runs shell commands and streams SSE events."""

import logging
from typing import Any, AsyncIterator

from loopy.abstract import LoopyContainerManager, LoopyHandler
from loopy.container.types import ExitChunk, StderrChunk, StdoutChunk
from loopy.api_model.request import ContainerRequest
from loopy.util.constants import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


class InvokeAgentRuntimeCommandHandler(LoopyHandler):
    def __init__(self, container_manager: LoopyContainerManager) -> None:
        super().__init__()
        self._container_manager = container_manager

    async def handle(self, request: ContainerRequest, context: Any = None) -> AsyncIterator[dict[str, Any]]:
        payload = request.invokeAgentRuntimeCommandPayload
        if not payload or not payload.command:
            raise ValueError("invokeAgentRuntimeCommandPayload is missing required fields")

        await self._container_manager.ensure_started(request.credentials)

        command = payload.command
        timeout = payload.timeout or DEFAULT_TIMEOUT

        yield {"event": {"contentStart": {}}}

        exit_code = -1
        status = "COMPLETED"

        async for chunk in self._container_manager.run_async(command, timeout):
            match chunk:
                case StdoutChunk(text=text):
                    yield {"event": {"contentDelta": {"stdout": text}}}
                case StderrChunk(text=text):
                    yield {"event": {"contentDelta": {"stderr": text}}}
                case ExitChunk(exit_code=code, timed_out=to):
                    exit_code = code
                    if to:
                        status = "TIMED_OUT"

        yield {"event": {"contentStop": {"exitCode": exit_code, "status": status}}}
