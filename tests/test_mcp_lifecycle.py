from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import anyio
import pytest
from mcp.server import MCPServer
from mcp.types import Tool, ToolAnnotations

from fakes import FakeLLMProvider, FakeToolExecutor
from minder_harness.core import (
    AgentHarness,
    CancellationToken,
    ExecutionLimits,
    Message,
    ModelResponse,
    Session,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
)
from minder_harness.core.ports import ModelExecutionContext
from minder_harness.mcp import MCPClientError, MCPToolExecutor, SDKMCPClient
from test_mcp import STATUS_TOOL, FakeMCPClient

pytestmark = pytest.mark.anyio


async def ignore_events(*args: object) -> None:
    pass


class CountingServer(MCPServer):
    listings = 0

    async def list_tools(self) -> list[Tool]:
        self.listings += 1
        return await super().list_tools()


async def test_shared_session_cancel_isolated_and_catalog_refresh() -> None:
    server = CountingServer("shared")
    slow_started = anyio.Event()
    release_other = anyio.Event()
    other_started = anyio.Event()
    values: list[str] = []

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def probe(kind: str) -> dict[str, str]:
        values.append(kind)
        if kind == "slow":
            slow_started.set()
            await anyio.sleep(30)
        if kind == "other":
            other_started.set()
            await release_other.wait()
        return {"kind": kind}

    token = CancellationToken()
    with anyio.fail_after(5):
        async with SDKMCPClient(server) as client:
            executor = MCPToolExecutor(client, ["probe"], max_attempts=1)
            await executor.discover_tools()
            slow = asyncio.create_task(
                executor.execute(
                    ToolCall("a", "probe", {"kind": "slow"}),
                    ToolExecutionContext(token, 3, ignore_events),
                )
            )
            other = asyncio.create_task(
                executor.execute(
                    ToolCall("b", "probe", {"kind": "other"}),
                )
            )
            await slow_started.wait()
            await other_started.wait()
            token.cancel()
            cancelled = await slow
            assert cancelled.error is not None and cancelled.error.code == "TOOL_CANCELLED"
            assert not other.done()
            assert client.is_ready
            release_other.set()
            assert (await other).status == "succeeded"
            assert (
                await executor.execute(ToolCall("c", "probe", {"kind": "fast"}))
            ).status == "succeeded"
            assert server.listings == 1
            await executor.discover_tools(refresh=True)
            assert server.listings == 2
        assert not client.is_ready
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "mcp-session"]
    assert values == ["slow", "other", "fast"]


async def test_capacity_wait_timeout_does_not_dispatch_or_close_session() -> None:
    server = CountingServer("capacity")
    entered = anyio.Event()
    release = anyio.Event()
    calls = 0

    @server.tool()
    async def probe() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": True}

    with anyio.fail_after(5):
        async with SDKMCPClient(server, max_in_flight=1) as client:
            first = asyncio.create_task(client.call_tool("probe", {}, 3))
            await entered.wait()
            with pytest.raises(MCPClientError) as caught:
                await client.call_tool("probe", {}, 0.04)
            assert caught.value.code == "MCP_TIMEOUT"
            assert caught.value.details["outcome_unknown"] is False
            assert calls == 1 and client.is_ready
            release.set()
            assert (await first).is_error is False


async def test_stale_catalog_rejected_before_dispatch() -> None:
    server = CountingServer("versions")
    async with SDKMCPClient(server) as client:
        version = client.catalog_version
        await client.list_tools(1, refresh=True)
        with pytest.raises(MCPClientError) as caught:
            await client.call_tool("missing", {}, 1, expected_catalog_version=version)
        assert caught.value.code == "MCP_CATALOG_CHANGED"
        assert caught.value.details["outcome_unknown"] is False


@pytest.mark.parametrize(
    "change, code",
    [
        ("removed", "TOOL_NOT_FOUND"),
        ("schema", "INVALID_TOOL_ARGUMENTS"),
        ("unsafe", "TOOL_RETRY_UNSAFE"),
    ],
)
async def test_reconnect_revalidates_before_retry(change: str, code: str) -> None:
    client = FakeMCPClient([STATUS_TOOL], [ConnectionError("disconnected")])

    def disconnect(_: int) -> None:
        client.catalog_version += 1
        if change == "removed":
            client.tools = ()
        elif change == "schema":
            definition = replace(
                STATUS_TOOL.definition,
                parameters={
                    "type": "object",
                    "required": ["new_required_field"],
                },
            )
            client.tools = (replace(STATUS_TOOL, definition=definition),)
        else:
            client.tools = (replace(STATUS_TOOL, retry_safe=False),)

    client.on_call = disconnect
    executor = MCPToolExecutor(client, ["get_machine_status"], retry_delay_seconds=0)
    result = await executor.execute(ToolCall("one", "get_machine_status", {"machine_id": "CNC-04"}))
    assert result.error is not None and result.error.code == code
    assert result.metadata["attempt_count"] == 1
    assert len(client.calls) == 1 and client.list_calls == 2


async def test_provider_cancel_releases_session_and_rejects_concurrent_run() -> None:
    entered = anyio.Event()

    class SlowProvider:
        async def generate(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            context: ModelExecutionContext | None = None,
        ) -> ModelResponse:
            entered.set()
            await anyio.sleep(30)
            return ModelResponse(content="late")

    harness = AgentHarness(SlowProvider(), FakeToolExecutor())
    session = Session("shared")
    token = CancellationToken()
    with anyio.fail_after(3):
        first = asyncio.create_task(harness.run(session, "first", cancellation=token))
        await entered.wait()
        with pytest.raises(ValueError, match="already active"):
            await harness.run(session, "second")
        token.cancel()
        run = await first
    assert run.status == "cancelled"
    assert run.output is None
    assert not any(message.content == "late" for message in session.messages)
    harness.provider = FakeLLMProvider([ModelResponse(content="ready")])
    assert (await harness.run(session, "again")).status == "completed"


async def test_provider_deadline_prevents_late_completion() -> None:
    class SlowProvider:
        async def generate(
            self,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition] = (),
            context: ModelExecutionContext | None = None,
        ) -> ModelResponse:
            await anyio.sleep(30)
            return ModelResponse(content="late")

    with anyio.fail_after(2):
        run = await AgentHarness(SlowProvider(), FakeToolExecutor()).run(
            Session("timeout"),
            "hello",
            limits=ExecutionLimits(timeout_seconds=0.03),
        )
    assert run.status == "limit_exceeded"
    assert run.output is None


async def test_reconnect_is_single_flight_and_short_waiter_does_not_cancel_startup() -> None:
    reconnect_started = anyio.Event()
    release = anyio.Event()

    class SlowReconnectServer(CountingServer):
        async def list_tools(self) -> list[Tool]:
            if self.listings == 1:
                reconnect_started.set()
                await release.wait()
            return await super().list_tools()

    server = SlowReconnectServer("reconnect")
    with anyio.fail_after(5):
        async with SDKMCPClient(server) as client:
            # Deterministic connection invalidation; real process death is tested by mcp_probe.
            assert client._client is not None
            client._invalidate(client._client)
            short = asyncio.create_task(client.list_tools(0.04))
            others = [asyncio.create_task(client.list_tools(2)) for _ in range(3)]
            await reconnect_started.wait()
            with pytest.raises(MCPClientError) as caught:
                await short
            assert caught.value.code == "MCP_TIMEOUT"
            release.set()
            assert await asyncio.gather(*others) == [(), (), ()]
            assert server.listings == 2 and client.is_ready


@pytest.mark.parametrize("operation", ["tool", "refresh"])
async def test_shutdown_interrupts_pending_requests_and_is_idempotent(operation: str) -> None:
    entered = anyio.Event()

    class SlowRefreshServer(CountingServer):
        async def list_tools(self) -> list[Tool]:
            if self.listings == 1:
                entered.set()
                await anyio.sleep(30)
            return await super().list_tools()

    server = SlowRefreshServer("shutdown")

    @server.tool()
    async def probe() -> dict[str, bool]:
        entered.set()
        await anyio.sleep(30)
        return {"late": True}

    with anyio.fail_after(5):
        async with SDKMCPClient(server) as client:
            request = asyncio.create_task(
                client.call_tool("probe", {}, 30)
                if operation == "tool"
                else client.list_tools(30, refresh=True)
            )
            await entered.wait()
            await client.aclose()
            with pytest.raises(MCPClientError) as caught:
                await request
            assert caught.value.code == "MCP_CONNECTION_LOST"
            assert not client.is_ready
        await client.aclose()
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "mcp-session"]
