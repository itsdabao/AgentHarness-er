from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
type RunStatus = Literal[
    "queued",
    "running",
    "cancel_requested",
    "completed",
    "failed",
    "cancelled",
    "limit_exceeded",
    "interrupted",
]
type MessageRole = Literal["system", "user", "assistant", "tool"]
type ToolResultStatus = Literal["succeeded", "failed"]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def as_jsonable(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {item.name: as_jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): as_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_jsonable(item) for item in value]
    raise TypeError(f"Value of type {type(value).__name__} is not JSON-compatible")


@dataclass(frozen=True)
class RuntimeError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, JSONValue]
    run_id: str | None = None
    execution_id: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.tool_call_id:
            raise ValueError("tool_call_id must not be empty")
        if not self.name:
            raise ValueError("tool name must not be empty")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, JSONValue]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must not be empty")


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    model_content: JSONValue
    error: RuntimeError | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("successful ToolResult cannot contain an error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed ToolResult must contain an error")

    def to_dict(self) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], as_jsonable(self))

    @classmethod
    def success(
        cls,
        tool_call: ToolCall,
        model_content: JSONValue,
        metadata: dict[str, JSONValue] | None = None,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
            status="succeeded",
            model_content=model_content,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def failure(
        cls,
        tool_call: ToolCall,
        error: RuntimeError,
        model_content: JSONValue | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.name,
            status="failed",
            model_content=(
                {"message": error.message, "error_code": error.code}
                if model_content is None
                else model_content
            ),
            error=error,
            metadata={} if metadata is None else metadata,
        )


@dataclass(frozen=True)
class Message:
    role: MessageRole
    content: JSONValue
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    message_id: str = field(default_factory=lambda: f"msg_{uuid4().hex}", compare=False)
    provider_data: dict[str, JSONValue] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class ModelResponse:
    content: JSONValue | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_data: dict[str, JSONValue] = field(default_factory=dict, repr=False)
    usage: dict[str, int] = field(default_factory=dict)

    def validation_error(self) -> RuntimeError | None:
        if self.content is None and not self.tool_calls:
            return RuntimeError(
                code="MALFORMED_MODEL_RESPONSE",
                message="The provider returned neither final content nor tool calls.",
            )
        if self.content is not None and self.tool_calls:
            return RuntimeError(
                code="MALFORMED_MODEL_RESPONSE",
                message="The provider returned final content and tool calls together.",
            )
        for call in self.tool_calls:
            if not call.tool_call_id or not call.name:
                return RuntimeError(
                    code="INVALID_MODEL_TOOL_CALL",
                    message="The provider returned a tool call without an id or name.",
                )
        return None


@dataclass(frozen=True)
class ExecutionLimits:
    max_steps: int = 8
    max_tool_calls: int = 8
    timeout_seconds: float | None = 120.0

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")


@dataclass
class Usage:
    model_steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Event:
    event_id: str
    session_id: str
    run_id: str
    sequence: int
    type: str
    timestamp: str
    payload: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], as_jsonable(self))


@dataclass
class Session:
    session_id: str
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass
class Run:
    run_id: str
    session_id: str
    status: RunStatus
    limits: ExecutionLimits
    usage: Usage = field(default_factory=Usage)
    output: JSONValue | None = None
    error: RuntimeError | None = None
    events: list[Event] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested_at: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], as_jsonable(self))
