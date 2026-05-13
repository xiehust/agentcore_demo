"""Hook provider that enforces execution limits (maxIterations, maxTokens, timeoutSeconds) on the agent loop."""

import logging
import time
from typing import Optional

from strands.hooks import BeforeModelCallEvent
from strands.hooks.registry import HookProvider, HookRegistry
from strands.types.exceptions import EventLoopException

from loopy.api_model.generated import HarnessStopReason

logger = logging.getLogger(__name__)


class ExecutionLimitExceeded(Exception):
    """Raised when an execution limit is exceeded."""

    def __init__(self, message: str, stop_reason: HarnessStopReason) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


class ExecutionLimitsHook(HookProvider):
    """Enforces maxIterations, maxTokens, and timeoutSeconds by checking before each model call."""

    def __init__(
        self,
        max_iterations: Optional[int] = None,
        max_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        self._timeout_seconds = timeout_seconds
        self._iteration_count = 0
        self._start_time = time.monotonic()

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._check_limits)

    def _check_limits(self, event: BeforeModelCallEvent) -> None:
        self._iteration_count += 1

        if self._max_iterations is not None and self._iteration_count > self._max_iterations:
            raise EventLoopException(
                ExecutionLimitExceeded(f"Max iterations exceeded: {self._max_iterations}", HarnessStopReason.max_iterations_exceeded)
            )

        if self._timeout_seconds is not None:
            elapsed = time.monotonic() - self._start_time
            if elapsed > self._timeout_seconds:
                raise EventLoopException(
                    ExecutionLimitExceeded(
                        f"Timeout exceeded: {self._timeout_seconds}s (elapsed {elapsed:.1f}s)",
                        HarnessStopReason.timeout_exceeded,
                    )
                )

        if self._max_tokens is not None:
            used = event.agent.event_loop_metrics.accumulated_usage.get("outputTokens", 0)
            if used >= self._max_tokens:
                raise EventLoopException(
                    ExecutionLimitExceeded(
                        f"Max tokens exceeded: {used}/{self._max_tokens}", HarnessStopReason.max_output_tokens_exceeded
                    )
                )
