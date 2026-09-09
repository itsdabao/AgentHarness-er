from __future__ import annotations

from collections.abc import Callable, Sequence
from threading import Timer
from typing import cast

import pytest

from fakes import FakeLLMProvider
from minder_harness.core import (
    AgentHarness,
    CancellationToken,
    ModelResponse,
    Session,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
)
from minder_harness.core.models import JSONValue
from minder_harness.mcp import (
    DiscoveredTool,
    MCPClientBackend,
    MCPClientError,
    MCPToolExecutor,
    MCPToolResponse,
    SDKMCPClient,
)
from minder_mock_mcp.server import build_server

pytestmark = pytest.mark.anyio

STATUS_TOOL = DiscoveredTool(
    ToolDefinition(
        name="get_machine_status",
        description="Get machine status.",
        parameters={
            "type": "object",
            "properties": {"machine_id": {"type": "string"}},
            "required": ["machine_id"],
            "additionalProperties": False,
        },
    ),
    retry_safe=True,
)

UPDATE_TOOL = DiscoveredTool(
    ToolDefinition(
        name="update_machine_mode",
        description="Change a machine mode.",
        parameters={
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
            "additionalProperties": False,
        },
    ),
    retry_safe=False,
)


class FakeMCPClient(MCPClientBackend):
    catalog_version = 0

    def __init__(
        self,
        tools: Sequence[DiscoveredTool],
        responses: Sequence[object] = (),
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self.tools = tuple(tools)
        self.responses = list(responses)
        self.on_call = on_call
        self.list_calls = 0
        self.calls: list[tuple[str, dict[str, JSONValue], float]] = []

    async def list_tools(
        self,
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[DiscoveredTool, ...]:
        self.list_calls += 1
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JSONValue],
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        expected_catalog_version: int | None = None,
    ) -> MCPToolResponse:
        self.calls.append((name, arguments, timeout_seconds))
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if not self.responses:
            raise AssertionError("FakeMCPClient has no scripted response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(MCPToolResponse, response)


def execution_context(
    events: list[tuple[str, dict[str, JSONValue]]],
    *,
    token: CancellationToken | None = None,
    timeout_seconds: float = 1.0,
) -> ToolExecutionContext:
    async def emit(event_type: str, payload: dict[str, JSONValue] | None = None) -> None:
        events.append((event_type, {} if payload is None else payload))

    return ToolExecutionContext(
        cancellation=CancellationToken() if token is None else token,
        timeout_seconds=timeout_seconds,
        emit=emit,
    )


async def test_discovery_exposes_only_approved_tools() -> None:
    client = FakeMCPClient([STATUS_TOOL, UPDATE_TOOL])
    executor = MCPToolExecutor(client, ["get_machine_status"])

    discovered = await executor.discover_tools()
    result = await executor.execute(ToolCall("call_1", "update_machine_mode", {"mode": "idle"}))

    assert [tool.name for tool in discovered] == ["get_machine_status"]
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_APPROVED"
    assert client.calls == []


async def test_unapproved_tool_is_visible_in_run_events() -> None:
    call = ToolCall("call_1", "update_machine_mode", {"mode": "idle"})
    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(call,)),
            ModelResponse(content="The requested tool is not approved."),
        ]
    )
    client = FakeMCPClient([STATUS_TOOL, UPDATE_TOOL])
    executor = MCPToolExecutor(client, ["get_machine_status"])

    run = await AgentHarness(provider, executor).run(Session("ses_1"), "Change machine mode")

    failure_event = next(event for event in run.events if event.type == "tool_execution_failed")
    assert failure_event.payload["tool_call_id"] == "call_1"
    assert failure_event.payload["error"] == {
        "code": "TOOL_NOT_APPROVED",
        "message": "Tool 'update_machine_mode' is not approved for this runtime.",
        "retryable": False,
        "details": {},
    }
    assert client.calls == []


async def test_approved_but_unknown_tool_is_rejected_locally() -> None:
    client = FakeMCPClient([STATUS_TOOL])
    executor = MCPToolExecutor(client, ["missing_tool"])

    result = await executor.execute(ToolCall("call_1", "missing_tool", {}))

    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"
    assert client.calls == []


async def test_invalid_arguments_do_not_reach_mcp() -> None:
    client = FakeMCPClient([STATUS_TOOL])
    executor = MCPToolExecutor(client, ["get_machine_status"])

    result = await executor.execute(ToolCall("call_1", "get_machine_status", {}))

    assert result.error is not None
    assert result.error.code == "INVALID_TOOL_ARGUMENTS"
    assert result.error.details["validation_errors"]
    assert client.calls == []


async def test_transient_safe_failure_retries_once_then_succeeds() -> None:
    client = FakeMCPClient(
        [STATUS_TOOL],
        [TimeoutError("slow server"), MCPToolResponse({"status": "warning"})],
    )
    executor = MCPToolExecutor(
        client,
        ["get_machine_status"],
        max_attempts=2,
        retry_delay_seconds=0,
    )
    events: list[tuple[str, dict[str, JSONValue]]] = []

    result = await executor.execute(
        ToolCall("call_1", "get_machine_status", {"machine_id": "CNC-04"}),
        execution_context(events),
    )

    assert result.status == "succeeded"
    assert len(client.calls) == 2
    assert result.metadata["attempt_count"] == 2
    assert [event_type for event_type, _ in events] == [
        "tool_execution_failed",
        "tool_execution_started",
    ]
    assert events[0][1]["will_retry"] is True
    assert events[0][1]["tool_call_id"] == "call_1"


async def test_max_attempts_one_disables_retry() -> None:
    client = FakeMCPClient([STATUS_TOOL], [ConnectionError("server dropped")])
    executor = MCPToolExecutor(client, ["get_machine_status"], max_attempts=1)

    result = await executor.execute(
        ToolCall("call_1", "get_machine_status", {"machine_id": "CNC-04"})
    )

    assert result.error is not None
    assert result.error.code == "MCP_CONNECTION_LOST"
    assert len(client.calls) == 1


async def test_remote_business_error_and_malformed_response_do_not_retry() -> None:
    remote_error = MCPToolResponse(
        {"message": "Unknown machine"},
        is_error=True,
        error_message="Unknown machine",
    )
    client = FakeMCPClient([STATUS_TOOL], [remote_error, object()])
    executor = MCPToolExecutor(client, ["get_machine_status"], retry_delay_seconds=0)
    call = ToolCall("call_1", "get_machine_status", {"machine_id": "UNKNOWN"})

    business_result = await executor.execute(call)
    malformed_result = await executor.execute(
        ToolCall("call_2", "get_machine_status", {"machine_id": "CNC-04"})
    )

    assert business_result.error is not None
    assert business_result.error.code == "MCP_TOOL_ERROR"
    assert business_result.metadata["attempts"] == [
        {"attempt": 1, "tool_call_id": "call_1", "status": "failed", "error_code": "MCP_TOOL_ERROR"}
    ]
    assert malformed_result.error is not None
    assert malformed_result.error.code == "MCP_MALFORMED_RESPONSE"
    assert len(client.calls) == 2


async def test_uncertain_side_effect_is_not_retried_or_replayed() -> None:
    client = FakeMCPClient([UPDATE_TOOL], [ConnectionError("connection reset")])
    executor = MCPToolExecutor(client, ["update_machine_mode"], retry_delay_seconds=0)
    first = ToolCall("call_1", "update_machine_mode", {"mode": "idle"})
    repeated = ToolCall("call_2", "update_machine_mode", {"mode": "idle"})

    first_result = await executor.execute(first)
    repeated_result = await executor.execute(repeated)

    assert first_result.error is not None
    assert first_result.error.code == "MCP_CONNECTION_LOST"
    assert repeated_result.error is not None
    assert repeated_result.error.code == "AMBIGUOUS_TOOL_OUTCOME"
    assert len(client.calls) == 1


async def test_cancellation_during_retry_wait_prevents_next_attempt() -> None:
    token = CancellationToken()
    client = FakeMCPClient([STATUS_TOOL], [TimeoutError("slow server")])
    executor = MCPToolExecutor(
        client,
        ["get_machine_status"],
        retry_delay_seconds=0.5,
    )
    timer = Timer(0.02, token.cancel)
    timer.start()
    try:
        result = await executor.execute(
            ToolCall("call_1", "get_machine_status", {"machine_id": "CNC-04"}),
            execution_context([], token=token),
        )
    finally:
        timer.cancel()

    assert result.error is not None
    assert result.error.code == "TOOL_CANCELLED"
    assert len(client.calls) == 1


async def test_remaining_deadline_prevents_retry_wait() -> None:
    client = FakeMCPClient([STATUS_TOOL], [TimeoutError("slow server")])
    executor = MCPToolExecutor(
        client,
        ["get_machine_status"],
        retry_delay_seconds=0.05,
    )

    result = await executor.execute(
        ToolCall("call_1", "get_machine_status", {"machine_id": "CNC-04"}),
        execution_context([], timeout_seconds=0.01),
    )

    assert result.error is not None
    assert result.error.code == "MCP_TIMEOUT"
    assert len(client.calls) == 1


async def test_agent_loop_uses_the_separate_mock_mcp_server() -> None:
    call = ToolCall("call_1", "get_machine_status", {"machine_id": "CNC-04"})
    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(call,)),
            ModelResponse(content="CNC-04 is in warning state."),
        ]
    )
    async with SDKMCPClient(build_server()) as client:
        executor = MCPToolExecutor(
            client,
            ["get_machine_status"],
            call_timeout_seconds=2,
            retry_delay_seconds=0,
        )
        tools = await executor.discover_tools()

        run = await AgentHarness(provider, executor).run(
            Session("ses_mcp"),
            "Check CNC-04",
            tools=tools,
        )

    assert run.status == "completed"
    assert provider.tool_definitions[0] == tools
    assert provider.calls[1][-1].content == {
        "machine_id": "CNC-04",
        "status": "warning",
        "temperature_c": 78.4,
        "area": "machining",
    }
    assert run.usage.tool_calls == 1


async def test_catalog_cached_until_explicit_refresh_but_results_are_fresh() -> None:
    client = FakeMCPClient(
        [STATUS_TOOL], [MCPToolResponse({"status": "warning"}), MCPToolResponse({"status": "ok"})]
    )
    executor = MCPToolExecutor(client, ["get_machine_status", "update_machine_mode"])
    assert await executor.discover_tools() == (STATUS_TOOL.definition,)
    client.tools = (STATUS_TOOL, UPDATE_TOOL)
    assert await executor.discover_tools() == (STATUS_TOOL.definition,)
    call = ToolCall("first", "get_machine_status", {"machine_id": "CNC-04"})
    assert (await executor.execute(call)).model_content == {"status": "warning"}
    assert (await executor.execute(call)).model_content == {"status": "ok"}
    assert client.list_calls == 1
    assert len(client.calls) == 2
    assert len(await executor.discover_tools(refresh=True)) == 2
    assert client.list_calls == 2


@pytest.mark.parametrize("discovery_duration", [0.04, 0.2])
async def test_discovery_consumes_run_deadline(
    monkeypatch: pytest.MonkeyPatch, discovery_duration: float
) -> None:
    clock = [10.0]
    monkeypatch.setattr("minder_harness.mcp.executor.monotonic", lambda: clock[0])
    token = CancellationToken()

    class DiscoveringClient(FakeMCPClient):
        async def list_tools(
            self,
            timeout_seconds: float,
            cancellation: CancellationToken | None = None,
            *,
            refresh: bool = False,
        ) -> tuple[DiscoveredTool, ...]:
            assert timeout_seconds == pytest.approx(0.1)
            assert cancellation is token
            clock[0] += discovery_duration
            return await super().list_tools(timeout_seconds, cancellation)

    client = DiscoveringClient([STATUS_TOOL], [MCPToolResponse({})])
    result = await MCPToolExecutor(client, ["get_machine_status"]).execute(
        ToolCall("first", "get_machine_status", {"machine_id": "CNC-04"}),
        execution_context([], token=token, timeout_seconds=0.1),
    )
    if discovery_duration < 0.1:
        assert result.status == "succeeded"
        assert client.calls[0][2] == pytest.approx(0.06)
    else:
        assert result.error is not None and result.error.code == "TOOL_TIMEOUT"
        assert client.calls == []


async def test_cancel_before_discovery_does_not_contact_server() -> None:
    token = CancellationToken()
    token.cancel()
    client = FakeMCPClient([STATUS_TOOL])
    result = await MCPToolExecutor(client, ["get_machine_status"]).execute(
        ToolCall("first", "get_machine_status", {"machine_id": "CNC-04"}),
        execution_context([], token=token),
    )
    assert result.error is not None and result.error.code == "TOOL_CANCELLED"
    assert client.list_calls == 0
    assert client.calls == []


@pytest.mark.parametrize("error_code", ["TOOL_CANCELLED", "MCP_MALFORMED_RESPONSE"])
async def test_model_repeat_cannot_bypass_uncertain_outcome(error_code: str) -> None:
    client = FakeMCPClient(
        [UPDATE_TOOL],
        [MCPClientError(error_code, "No reliable result", details={"outcome_unknown": True})],
    )
    executor = MCPToolExecutor(client, ["update_machine_mode"])
    provider = FakeLLMProvider(
        responses=[
            ModelResponse(tool_calls=(ToolCall("first", "update_machine_mode", {"mode": "idle"}),)),
            ModelResponse(
                tool_calls=(ToolCall("new_id", "update_machine_mode", {"mode": "idle"}),)
            ),
            ModelResponse(content="Outcome needs verification."),
        ]
    )
    run = await AgentHarness(provider, executor).run(Session("replay"), "Change mode")
    assert run.status == "completed"
    assert len(client.calls) == 1
    result = provider.calls[2][-1].content
    assert isinstance(result, dict) and result["error_code"] == "AMBIGUOUS_TOOL_OUTCOME"


async def test_known_failure_before_dispatch_does_not_poison_replay_guard() -> None:
    client = FakeMCPClient(
        [UPDATE_TOOL],
        [
            MCPClientError(
                "MCP_CONFIGURATION_ERROR", "Missing server", details={"outcome_unknown": False}
            ),
            MCPToolResponse({"mode": "idle"}),
        ],
    )
    executor = MCPToolExecutor(client, ["update_machine_mode"])
    call = ToolCall("first", "update_machine_mode", {"mode": "idle"})
    assert (await executor.execute(call)).status == "failed"
    assert (await executor.execute(call)).status == "succeeded"


async def test_deadline_expires_during_wait_without_phantom_attempt_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("minder_harness.mcp.executor.monotonic", lambda: clock[0])

    async def expire(context: ToolExecutionContext | None, delay: float) -> bool:
        clock[0] = 2.0
        return False

    monkeypatch.setattr("minder_harness.mcp.executor._wait_for_retry", expire)
    client = FakeMCPClient([STATUS_TOOL], [TimeoutError("slow")])
    events: list[tuple[str, dict[str, JSONValue]]] = []
    result = await MCPToolExecutor(client, ["get_machine_status"]).execute(
        ToolCall("first", "get_machine_status", {"machine_id": "CNC-04"}),
        execution_context(events),
    )
    assert result.error is not None and result.error.code == "TOOL_TIMEOUT"
    assert len(client.calls) == 1
    assert [kind for kind, _ in events] == ["tool_execution_failed"]


@pytest.mark.parametrize("failure", [PermissionError("denied"), ValueError("unknown")])
async def test_permanent_and_unknown_errors_do_not_retry(failure: Exception) -> None:
    client = FakeMCPClient([STATUS_TOOL], [failure])
    result = await MCPToolExecutor(client, ["get_machine_status"]).execute(
        ToolCall("first", "get_machine_status", {"machine_id": "CNC-04"})
    )
    assert result.error is not None and result.error.retryable is False
    assert len(client.calls) == 1
