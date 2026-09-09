from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .cancellation import CancellationToken
from .models import (
    JSONValue,
    Message,
    ModelResponse,
    RuntimeError,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

type EventEmitter = Callable[[str, dict[str, JSONValue] | None], Awaitable[object]]


class StorageError(Exception):
    """Recording failed; stop work rather than treating this as a tool failure."""


class RunConflict(ValueError):
    """The session already owns an active run."""


class ReplayBlocked(Exception):
    """A previous matching operation may have executed."""


class ProviderError(Exception):
    """An adapter-classified, safe provider failure; never an SDK exception."""

    def __init__(self, error: RuntimeError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class ModelExecutionContext:
    cancellation: CancellationToken
    deadline: float | None
    emit: EventEmitter
    step: int


class AttemptRecorder(Protocol):
    async def begin_attempt(
        self,
        call: ToolCall,
        scope: str,
        attempt: int,
        retry_safe: bool,
    ) -> str: ...

    async def end_attempt(
        self,
        attempt_id: str,
        result: ToolResult,
        outcome_unknown: bool,
    ) -> None: ...


@dataclass(frozen=True)
class ToolExecutionContext:
    cancellation: CancellationToken
    timeout_seconds: float | None
    emit: EventEmitter
    recorder: AttemptRecorder | None = None


class LLMProvider(Protocol):
    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        """Return one provider-neutral model step."""


class ToolExecutor(Protocol):
    async def execute(
        self,
        tool_call: ToolCall,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """Execute one provider-neutral tool call."""
