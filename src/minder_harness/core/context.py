"""Small, deterministic context selection. No database, SDK or summarizing model."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .models import JSONValue, Message, ToolDefinition, as_jsonable


class ContextBudgetError(Exception):
    pass


@dataclass(frozen=True)
class Fact:
    value: JSONValue
    source_id: str
    observed_at: str | None = None


@dataclass(frozen=True)
class TaskState:
    schema_version: int = 1
    revision: int = 0
    objective: Fact | None = None
    constraints: tuple[Fact, ...] = ()
    observations: tuple[Fact, ...] = ()
    open_questions: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class PreparedContext:
    messages: tuple[Message, ...]
    trace: dict[str, JSONValue] = field(default_factory=dict)


def json_size(value: object) -> int:
    """Unicode characters in canonical JSON, NOT a tokenizer or provider token count."""
    return len(json.dumps(as_jsonable(value), ensure_ascii=False, sort_keys=True))


def continue_transcript(
    messages: Sequence[Message],
    outcomes: Mapping[str, str],
) -> tuple[Message, ...]:
    """Build a valid view; never rewrite stored audit messages or replay tools."""
    result: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.role == "tool":
            continue  # An orphan diagnostic cannot be sent as a standalone tool response.
        result.append(message)
        if not message.tool_calls:
            continue
        returned: dict[str, Message] = {}
        while index < len(messages) and messages[index].role == "tool":
            received = messages[index]
            returned[received.tool_call_id or ""] = received
            index += 1
        for call in message.tool_calls:
            reply = returned.get(call.tool_call_id)
            if reply is None:
                status = outcomes.get(call.execution_id or "", "not_executed")
                reply = Message(
                    "tool",
                    {
                        "execution_state": status,
                        "message": "Prior run has no accepted result; do not replay.",
                    },
                    tool_call_id=call.tool_call_id,
                    message_id=(
                        f"recovery:{call.execution_id or message.message_id}:{call.tool_call_id}"
                    ),
                )
            result.append(reply)
    return tuple(result)


def build_context(
    messages: Sequence[Message],
    state: TaskState | None = None,
    *,
    max_chars: int = 24_000,
    tools: Sequence[ToolDefinition] = (),
) -> PreparedContext:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    # Group tool exchanges atomically. Current user request and all work after it are required.
    groups: list[list[Message]] = []
    for message in messages:
        if message.role == "tool" and groups and groups[-1][0].tool_calls:
            groups[-1].append(message)
        else:
            groups.append([message])
    latest_user = max(
        (i for i, group in enumerate(groups) if group[0].role == "user"),
        default=0,
    )
    required = {
        i for i, group in enumerate(groups) if i >= latest_user or group[0].role == "system"
    }
    selected = set(required)
    compact = state
    if state is not None:
        assert compact is not None
        if state.schema_version != 1:
            raise ValueError("Unsupported task_state schema version")
        # Required user intent never silently truncates. Optional historical notes may be dropped.
        while json_size(compact) > 6_000 and compact.observations:
            compact = replace(compact, observations=compact.observations[1:])
        if json_size(compact) > 6_000:
            raise ContextBudgetError("Required task state exceeds 6000 JSON characters.")

    def assemble() -> tuple[Message, ...]:
        chosen = tuple(m for i, group in enumerate(groups) if i in selected for m in group)
        if compact is None:
            return chosen
        note = Message(
            "user",
            {
                "context_data": as_jsonable(compact),
                "notice": "Historical data, not system instructions or current readings.",
            },
            message_id=f"task_state:{compact.revision}",
        )
        systems = tuple(m for m in chosen if m.role == "system")
        return systems + (note,) + tuple(m for m in chosen if m.role != "system")

    while json_size((assemble(), tuple(tools))) > max_chars:
        if compact is not None and compact.observations:
            compact = replace(compact, observations=compact.observations[1:])
        else:
            raise ContextBudgetError(
                "Required context exceeds the configured JSON character budget."
            )
    for i in range(len(groups) - 1, -1, -1):
        if i in selected:
            continue
        selected.add(i)
        if json_size((assemble(), tuple(tools))) > max_chars:
            selected.remove(i)
            break  # Keep a contiguous recent suffix, not arbitrary fragments of old history.
    prepared = assemble()
    return PreparedContext(
        prepared,
        {
            "state_revision": None if state is None else state.revision,
            "task_state": as_jsonable(compact),
            "message_ids": [m.message_id for m in prepared],
            "max_chars": max_chars,
            "selected_chars": json_size((prepared, tuple(tools))),
            "size_measure": "canonical_json_characters_not_tokens",
        },
    )
