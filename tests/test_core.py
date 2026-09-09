from __future__ import annotations

import json
from collections.abc import Sequence
from unittest.mock import Mock

import pytest

from fakes import FakeLLMProvider, FakeToolExecutor
from minder_harness.core import (
    AgentHarness,
    CancellationToken,
    ExecutionLimits,
    Message,
    ModelResponse,
    RuntimeError,
    Session,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    render_event_trace,
)
from minder_harness.core.context import Fact, TaskState
from minder_harness.core.ports import AttemptRecorder, ModelExecutionContext

pytestmark = pytest.mark.anyio


async def test_loop_forwards_async_context_and_records_continuation() -> None:
    token = CancellationToken()
    recorder = Mock(spec=AttemptRecorder)
    call = ToolCall("contract-call", "get_status", {})
    session = Session("contract-session")
    state = TaskState(objective=Fact("Check status", "source"))
    model_contexts: list[ModelExecutionContext] = []

    class Provider(FakeLLMProvider):
        async def generate(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            context: ModelExecutionContext | None = None,
        ) -> ModelResponse:
            assert context is not None
            assert context.cancellation is token and context.deadline is not None
            model_contexts.append(context)
            return await super().generate(messages, tools, context)

    class Executor(FakeToolExecutor):
        async def execute(
            self, tool_call: ToolCall, context: ToolExecutionContext | None = None
        ) -> ToolResult:
            assert context is not None and context.recorder is recorder
            assert context.cancellation is token
            assert context.timeout_seconds is not None and context.timeout_seconds > 0
            assert tool_call.run_id and tool_call.execution_id
            return await super().execute(tool_call, context)

    provider = Provider(
        responses=[
            ModelResponse(tool_calls=(call,), provider_data={"opaque": "continuation"}),
            ModelResponse(content="done", usage={"input_tokens": 12, "output_tokens": 3}),
        ]
    )
    executor = Executor({call.tool_call_id: ToolResult.success(call, {"status": "ok"})})
    run = await AgentHarness(provider, executor).run(
        session,
        "Check status",
        cancellation=token,
        recorder=recorder,
        task_state=state,
        context_limit=8000,
    )

    assert run.status == "completed"
    assert [ctx.step for ctx in model_contexts] == [1, 2]
    assert model_contexts[0].deadline == model_contexts[1].deadline
    assert executor.calls[0].run_id == run.run_id
    assert run.usage.input_tokens == 12 and run.usage.output_tokens == 3
    assert session.messages[1].provider_data == {"opaque": "continuation"}
    requests = [e for e in run.events if e.type == "model_request_started"]
    context_trace = requests[0].payload["context"]
    assert isinstance(context_trace, dict)
    assert context_trace["max_chars"] == 8000
    assert requests[1].payload["phase"] == "processing_tool_results"
    assert any(m.message_id == "task_state:0" for m in provider.calls[0])
    assert all(
        "_message" in e.payload
        for e in run.events
        if e.type in {"model_response_received", "tool_execution_completed"}
    )


async def test_multi_step_loop_feeds_tool_result_to_next_model_step() -> None:
    call = ToolCall("call_001", "get_status", {"machine_id": "CNC-04"})
    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(call,)),
            ModelResponse(content="Machine is in warning state."),
        ]
    )
    executor = FakeToolExecutor(
        {call.tool_call_id: ToolResult.success(call, {"status": "warning"})}
    )

    run = await AgentHarness(provider, executor).run(Session("ses_001"), "Check CNC-04")

    assert run.status == "completed"
    assert run.output == "Machine is in warning state."
    assert len(provider.calls) == 2
    assert provider.calls[1][-1] == Message(
        role="tool",
        content={"status": "warning"},
        tool_call_id=call.tool_call_id,
    )
    assert [event.type for event in run.events] == [
        "run_started",
        "model_request_started",
        "model_response_received",
        "tool_call_requested",
        "tool_execution_started",
        "tool_execution_completed",
        "model_request_started",
        "model_response_received",
        "run_completed",
    ]


async def test_final_response_completes_without_tools() -> None:
    provider = FakeLLMProvider(responses=[ModelResponse(content="done")])
    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert run.status == "completed"
    assert run.output == "done"
    assert len(provider.calls) == 1
    assert len(run.events) == 4


async def test_tool_failure_is_returned_to_the_next_model_call() -> None:
    call = ToolCall("call_002", "get_status", {"machine_id": "CNC-04"})
    failure = RuntimeError("TOOL_FAILED", "Machine status is unavailable.")
    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(call,)),
            ModelResponse(content="I could not retrieve the status."),
        ]
    )
    executor = FakeToolExecutor({call.tool_call_id: ToolResult.failure(call, failure)})

    run = await AgentHarness(provider, executor).run(Session("ses_001"), "Check CNC-04")

    assert run.status == "completed"
    assert provider.calls[1][-1].role == "tool"
    assert provider.calls[1][-1].content == {
        "message": "Machine status is unavailable.",
        "error_code": "TOOL_FAILED",
    }
    assert "tool_execution_failed" in [event.type for event in run.events]


async def test_provider_failure_becomes_structured_run_failure() -> None:
    class FailingProvider:
        async def generate(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            context: ModelExecutionContext | None = None,
        ) -> ModelResponse:
            raise ValueError("provider down")

    provider = FailingProvider()
    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert run.status == "failed"
    assert run.error is not None
    assert run.error.code == "PROVIDER_ERROR"
    assert run.events[-1].type == "run_failed"


async def test_provider_timeout_becomes_structured_timeout() -> None:
    class TimeoutProvider:
        async def generate(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            context: ModelExecutionContext | None = None,
        ) -> ModelResponse:
            raise TimeoutError("provider deadline")

    run = await AgentHarness(TimeoutProvider(), FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert run.status == "failed"
    assert run.error is not None
    assert run.error.code == "PROVIDER_TIMEOUT"


async def test_tool_timeout_becomes_structured_tool_failure() -> None:
    call = ToolCall("call_timeout", "get_status", {})

    class TimeoutExecutor:
        async def execute(
            self,
            tool_call: ToolCall,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            raise TimeoutError("tool deadline")

    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(call,)),
            ModelResponse(content="The tool timed out."),
        ]
    )
    run = await AgentHarness(provider, TimeoutExecutor()).run(Session("ses_001"), "Check status")

    assert run.status == "completed"
    assert provider.calls[1][-1].content == {
        "message": "tool deadline",
        "error_code": "TOOL_TIMEOUT",
    }


async def test_malformed_provider_response_is_rejected() -> None:
    provider = FakeLLMProvider(responses=[ModelResponse()])
    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert run.status == "failed"
    assert run.error is not None
    assert run.error.code == "MALFORMED_MODEL_RESPONSE"


async def test_invalid_model_tool_call_is_rejected() -> None:
    call = ToolCall("call_003", "get_status", {})
    object.__setattr__(call, "name", "")
    provider = FakeLLMProvider(responses=[ModelResponse(tool_calls=(call,))])

    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert run.status == "failed"
    assert run.error is not None
    assert run.error.code == "INVALID_MODEL_TOOL_CALL"


async def test_infinite_tool_loop_stops_at_model_step_limit() -> None:
    call = ToolCall("call_forever", "get_status", {})
    provider = FakeLLMProvider(always=ModelResponse(tool_calls=(call,)))
    executor = FakeToolExecutor({call.tool_call_id: ToolResult.success(call, {"status": "ok"})})

    run = await AgentHarness(provider, executor).run(
        Session("ses_001"),
        "Keep checking",
        limits=ExecutionLimits(max_steps=3, max_tool_calls=10, timeout_seconds=None),
    )

    assert run.status == "limit_exceeded"
    assert run.error is not None
    assert run.error.code == "MAX_STEPS_EXCEEDED"
    assert run.usage.model_steps == 3
    assert run.usage.tool_calls == 2
    assert run.events[-1].type == "run_limit_exceeded"


async def test_tool_call_limit_stops_before_scheduling_more_tools() -> None:
    first = ToolCall("call_001", "get_status", {})
    second = ToolCall("call_002", "get_status", {})
    provider = FakeLLMProvider(responses=[ModelResponse(tool_calls=(first, second))])
    executor = FakeToolExecutor({first.tool_call_id: ToolResult.success(first, {"status": "ok"})})

    run = await AgentHarness(provider, executor).run(
        Session("ses_001"),
        "Check status",
        limits=ExecutionLimits(max_steps=4, max_tool_calls=1, timeout_seconds=None),
    )

    assert run.status == "limit_exceeded"
    assert [call.tool_call_id for call in executor.calls] == [first.tool_call_id]
    assert run.error is not None
    assert run.error.code == "MAX_TOOL_CALLS_EXCEEDED"


async def test_cancellation_between_steps_prevents_tool_scheduling() -> None:
    token = CancellationToken()
    call = ToolCall("call_cancelled", "get_status", {})
    provider = FakeLLMProvider(
        responses=[ModelResponse(tool_calls=(call,))],
        on_generate=lambda count: token.cancel() if count == 1 else None,
    )
    executor = FakeToolExecutor()

    run = await AgentHarness(provider, executor).run(
        Session("ses_001"),
        "Check status",
        cancellation=token,
    )

    assert run.status == "cancelled"
    assert executor.calls == []
    assert [event.type for event in run.events][-2:] == [
        "run_cancel_requested",
        "run_cancelled",
    ]


async def test_events_are_ordered_and_json_serializable() -> None:
    provider = FakeLLMProvider(responses=[ModelResponse(content="done")])
    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")

    assert [event.sequence for event in run.events] == [1, 2, 3, 4]
    serialized = [event.to_dict() for event in run.events]
    json.dumps(serialized)
    assert serialized[0]["type"] == "run_started"


async def test_terminal_renderer_is_read_only_and_human_readable() -> None:
    provider = FakeLLMProvider(responses=[ModelResponse(content="done")])
    run = await AgentHarness(provider, FakeToolExecutor()).run(Session("ses_001"), "Hello")
    before = list(run.events)

    trace = render_event_trace(run.events)

    assert "RUN " in trace
    assert "001 run_started" in trace
    assert "004 run_completed" in trace
    assert run.events == before
