"""JSON hydration for the existing dataclasses; SDK-independent."""

from __future__ import annotations

from typing import Any

from .context import Fact, TaskState
from .models import ExecutionLimits, Message, Run, RuntimeError, ToolCall, Usage


def message_from(data: dict[str, Any]) -> Message:
    return Message(
        role=data["role"],
        content=data["content"],
        tool_call_id=data.get("tool_call_id"),
        tool_calls=tuple(ToolCall(**call) for call in data.get("tool_calls", [])),
        message_id=data["message_id"],
        provider_data=data.get("provider_data", {}),
    )


def run_from(data: dict[str, Any]) -> Run:
    return Run(
        run_id=data["run_id"],
        session_id=data["session_id"],
        status=data["status"],
        limits=ExecutionLimits(**data["limits"]),
        usage=Usage(**data["usage"]),
        output=data.get("output"),
        error=None if data.get("error") is None else RuntimeError(**data["error"]),
        created_at=data["created_at"],
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        cancel_requested_at=data.get("cancel_requested_at"),
    )


def state_from(data: dict[str, Any]) -> TaskState:
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported task_state schema version")
    return TaskState(
        schema_version=1,
        revision=data["revision"],
        objective=None if data["objective"] is None else Fact(**data["objective"]),
        constraints=tuple(Fact(**item) for item in data["constraints"]),
        observations=tuple(Fact(**item) for item in data["observations"]),
        open_questions=tuple(Fact(**item) for item in data["open_questions"]),
    )
