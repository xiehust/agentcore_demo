"""Shared async subprocess streaming helper."""

import asyncio
from collections.abc import AsyncIterator

from loopy.container.types import ExitChunk, StderrChunk, StdoutChunk, StreamChunk


async def stream_subprocess(cmd: list[str], timeout: int) -> AsyncIterator[StreamChunk]:
    """Run a subprocess and yield typed chunks, then exit metadata."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    async def _timeout_killer():
        await asyncio.sleep(timeout)
        proc.kill()

    timer = asyncio.create_task(_timeout_killer())
    queue: asyncio.Queue = asyncio.Queue()

    async def _pipe_to_queue(stream, is_stderr):
        async for line in stream:
            text = line.decode("utf-8", errors="replace")
            await queue.put(StderrChunk(text) if is_stderr else StdoutChunk(text))
        await queue.put(None)

    asyncio.create_task(_pipe_to_queue(proc.stdout, False))
    asyncio.create_task(_pipe_to_queue(proc.stderr, True))

    done = 0
    while done < 2:
        item = await queue.get()
        if item is None:
            done += 1
        else:
            yield item

    await proc.wait()
    timer.cancel()
    timed_out = proc.returncode == -9
    yield ExitChunk(exit_code=-1 if timed_out else proc.returncode, timed_out=timed_out)
