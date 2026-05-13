"""Types for container manager streaming output."""

from dataclasses import dataclass
from typing import Union


@dataclass
class StdoutChunk:
    text: str


@dataclass
class StderrChunk:
    text: str


@dataclass
class ExitChunk:
    exit_code: int
    timed_out: bool


StreamChunk = Union[StdoutChunk, StderrChunk, ExitChunk]
