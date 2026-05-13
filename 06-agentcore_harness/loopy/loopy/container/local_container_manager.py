"""Local container manager — runs commands directly via subprocess."""

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from loopy.abstract import FileIO, LoopyContainerManager
from loopy.api_model.request import Credentials
from loopy.container.stream import stream_subprocess
from loopy.container.types import StreamChunk


class LocalFileIO(FileIO):
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        Path(path).write_text(content, encoding="utf-8")

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def listdir(self, path: str) -> str:
        items = []
        for item in sorted(Path(path).iterdir()):
            items.append(f"{item.name}/" if item.is_dir() else item.name)
        return "\n".join(items)

    def mkdir_parents(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)


class LocalContainerManager(LoopyContainerManager):
    """Default — runs commands and file operations directly on the Loopy container."""

    async def ensure_started(self, credentials: Optional[Credentials] = None) -> None:
        pass

    def run(self, command: str, timeout: int = 300) -> dict[str, Any]:
        try:
            result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=timeout)
            return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": f"Command timed out after {timeout} seconds", "exit_code": -1}

    async def run_async(self, command: str, timeout: int = 300) -> AsyncIterator[StreamChunk]:
        async for chunk in stream_subprocess(["/bin/bash", "-c", command], timeout):
            yield chunk

    @property
    def is_customer_container(self) -> bool:
        return False

    @property
    def file_io(self) -> FileIO:
        return LocalFileIO()
