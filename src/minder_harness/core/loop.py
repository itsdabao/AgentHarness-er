from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import partial
from time import monotonic
from typing import Literal
from uuid import uuid4

from .cancellation import CancellationToken, OperationCancelled, cancellable
from .context import ContextBudgetError, TaskState, build_context
from .models import (
    ExecutionLimits,
    JSONValue,
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
    Usage,
    as_jsonable,
)
from .models import (
    RuntimeError as AgentRuntimeError,
)
from .ports import (
    AttemptRecorder,
    EventEmitter,
    LLMProvider,
    ModelExecutionContext,
    ProviderError,
    StorageError,
    ToolExecutionContext,
    ToolExecutor,
)

LoopStatus = Literal["completed", "failed", "cancelled", "limit_exceeded"]


@dataclass(frozen=True)
class LoopResult:
    status: LoopStatus
    messages: tuple[Message, ...]
    usage: Usage
    output: JSONValue | None = None
    error: AgentRuntimeError | None = None


class AgentLoop:
    """The transport-independent model/tool/result execution algorithm."""

    async def run(
        self,
        messages: Sequence[Message],
        provider: LLMProvider,
        tool_executor: ToolExecutor,
        limits: ExecutionLimits,
        cancellation: CancellationToken,
        emit: EventEmitter,
        tools: Sequence[ToolDefinition] = (),
        *,
        run_id: str | None = None,
        recorder: AttemptRecorder | None = None,
        task_state: TaskState | None = None,
        context_limit: int = 24_000,
    ) -> LoopResult:
        working_messages = list(messages)
        usage = Usage()
        started = monotonic()
        cancel_event_emitted = False

        async def cancelled() -> LoopResult:
            nonlocal cancel_event_emitted
            if not cancel_event_emitted:
                await emit("run_cancel_requested", {})
                cancel_event_emitted = True
            return LoopResult("cancelled", tuple(working_messages), usage)

        def limit_exceeded(code: str, message: str) -> LoopResult:
            return LoopResult(
                "limit_exceeded",
                tuple(working_messages),
                usage,
                error=AgentRuntimeError(code=code, message=message),
            )

        def timed_out() -> bool:
            return (
                limits.timeout_seconds is not None
                and monotonic() - started >= limits.timeout_seconds
            )

        def remaining_seconds() -> float | None:
            if limits.timeout_seconds is None:
                return None
            return max(0.0, limits.timeout_seconds - (monotonic() - started))

        while True:
            if cancellation.is_cancelled:
                return await cancelled()
            if timed_out():
                return limit_exceeded(
                    "EXECUTION_DEADLINE_EXCEEDED",
                    "The overall execution deadline was exceeded.",
                )
            if usage.model_steps >= limits.max_steps:
                return limit_exceeded(
                    "MAX_STEPS_EXCEEDED",
                    "The maximum number of model steps was exceeded.",
                )

            try:
                prepared = build_context(
                    working_messages, task_state, max_chars=context_limit, tools=tools
                )
            except ContextBudgetError as exc:
                return LoopResult(
                    "failed",
                    tuple(working_messages),
                    usage,
                    error=AgentRuntimeError("CONTEXT_BUDGET_EXCEEDED", str(exc)),
                )
            usage.model_steps += 1
            await emit(
                "model_request_started",
                {
                    "step": usage.model_steps,
                    "context": prepared.trace,
                    "phase": "processing_tool_results"
                    if working_messages[-1].role == "tool"
                    else "awaiting_model",
                },
            )
            try:
                response = await cancellable(
                    partial(
                        provider.generate,
                        prepared.messages,
                        tools,
                        ModelExecutionContext(
                            cancellation,
                            None
                            if limits.timeout_seconds is None
                            else started + limits.timeout_seconds,
                            emit,
                            usage.model_steps,
                        ),
                    ),
                    remaining_seconds(),
                    cancellation,
                )
            except OperationCancelled:
                return await cancelled()
            except StorageError:
                raise
            except ProviderError as exc:
                return LoopResult("failed", tuple(working_messages), usage, error=exc.error)
            except TimeoutError:
                if timed_out():
                    return limit_exceeded(
                        "EXECUTION_DEADLINE_EXCEEDED",
                        "The overall execution deadline was exceeded.",
                    )
                return LoopResult(
                    "failed",
                    tuple(working_messages),
                    usage,
                    error=AgentRuntimeError(
                        "PROVIDER_TIMEOUT", "The model provider timed out.", retryable=True
                    ),
                )
            except Exception:
                return LoopResult(
                    "failed",
                    tuple(working_messages),
                    usage,
                    error=AgentRuntimeError(
                        "PROVIDER_ERROR", "The model provider failed unexpectedly."
                    ),
                )

            if cancellation.is_cancelled:
                return await cancelled()
            if timed_out():
                return limit_exceeded(
                    "EXECUTION_DEADLINE_EXCEEDED", "The overall execution deadline was exceeded."
                )
            if not isinstance(response, ModelResponse):
                return LoopResult(
                    "failed",
                    tuple(working_messages),
                    usage,
                    error=AgentRuntimeError(
                        "MALFORMED_MODEL_RESPONSE", "Invalid provider response type."
                    ),
                )
            validation_error = response.validation_error()
            if validation_error is not None:
                await emit(
                    "model_response_received",
                    {
                        "step": usage.model_steps,
                        "response": as_jsonable(response),
                        "validation_error": as_jsonable(validation_error),
                    },
                )
                return LoopResult("failed", tuple(working_messages), usage, error=validation_error)

            response = replace(
                response,
                tool_calls=tuple(
                    replace(call, run_id=run_id, execution_id=f"exec_{uuid4().hex}")
                    for call in response.tool_calls
                ),
            )
            usage.input_tokens += response.usage.get("input_tokens", 0)
            usage.output_tokens += response.usage.get("output_tokens", 0)
            message = Message(
                "assistant",
                response.content,
                tool_calls=response.tool_calls,
                provider_data=response.provider_data,
            )
            await emit(
                "model_response_received",
                {
                    "step": usage.model_steps,
                    "has_tool_calls": bool(response.tool_calls),
                    "tool_call_count": len(response.tool_calls),
                    "response": as_jsonable(response),
                    "_message": as_jsonable(message),
                },
            )
            if response.content is not None:
                if cancellation.is_cancelled:
                    return await cancelled()
                if timed_out():
                    return limit_exceeded(
                        "EXECUTION_DEADLINE_EXCEEDED",
                        "Deadline expired during response recording.",
                    )
                working_messages.append(message)
                return LoopResult(
                    "completed", tuple(working_messages), usage, output=response.content
                )

            working_messages.append(message)
            if usage.model_steps >= limits.max_steps:
                return limit_exceeded(
                    "MAX_STEPS_EXCEEDED",
                    "The model requested tools on the final allowed step.",
                )

            for tool_call in response.tool_calls:
                if cancellation.is_cancelled:
                    return await cancelled()
                if timed_out():
                    return limit_exceeded(
                        "EXECUTION_DEADLINE_EXCEEDED",
                        "The overall execution deadline was exceeded.",
                    )
                if usage.tool_calls >= limits.max_tool_calls:
                    return limit_exceeded(
                        "MAX_TOOL_CALLS_EXCEEDED",
                        "The maximum number of tool calls was exceeded.",
                    )

                usage.tool_calls += 1
                await emit("tool_call_requested", _tool_call_payload(tool_call))
                await emit("tool_execution_started", _tool_call_payload(tool_call) | {"attempt": 1})
                try:
                    # The executor owns tool I/O cancellation and durable attempt outcomes.
                    result = await tool_executor.execute(
                        tool_call,
                        ToolExecutionContext(
                            cancellation, remaining_seconds(), emit, recorder=recorder
                        ),
                    )
                    if not isinstance(result, ToolResult):
                        raise TypeError("ToolExecutor returned an invalid ToolResult")
                except StorageError:
                    raise
                except OperationCancelled:
                    return await cancelled()
                except TimeoutError as exc:
                    result = ToolResult.failure(
                        tool_call,
                        AgentRuntimeError(
                            "TOOL_TIMEOUT",
                            str(exc) or "The tool executor timed out.",
                            retryable=True,
                        ),
                    )
                except Exception as exc:
                    result = ToolResult.failure(
                        tool_call,
                        AgentRuntimeError(
                            "TOOL_EXECUTION_ERROR",
                            str(exc) or "The tool executor failed.",
                            retryable=True,
                        ),
                    )

                reply = Message("tool", result.model_content, tool_call_id=result.tool_call_id)
                payload = result.to_dict() | {
                    "execution_id": tool_call.execution_id,
                    "_message": as_jsonable(reply),
                }
                await emit(
                    "tool_execution_failed"
                    if result.status == "failed"
                    else "tool_execution_completed",
                    payload,
                )
                if cancellation.is_cancelled:
                    return await cancelled()
                if timed_out():
                    return limit_exceeded(
                        "EXECUTION_DEADLINE_EXCEEDED",
                        "The overall execution deadline was exceeded.",
                    )
                working_messages.append(reply)


def _tool_call_payload(tool_call: ToolCall) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "tool_call_id": tool_call.tool_call_id,
        "tool_name": tool_call.name,
        "arguments": tool_call.arguments,
        "execution_id": tool_call.execution_id,
    }
    if tool_call.run_id is not None:
        payload["run_id"] = tool_call.run_id
    return payload
