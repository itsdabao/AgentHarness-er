"""Small test-only lifecycle and fault helpers; assertions stay in the tests."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import pytest

from fakes import FakeLLMProvider, FakeToolExecutor
from minder_harness.core import AgentHarness, ExecutionLimits, Message, Run, Session, ToolDefinition
from minder_harness.core.context import Fact, TaskState
from minder_harness.core.ports import LLMProvider, ToolExecutor
from minder_harness.mcp import MCPToolExecutor, SDKMCPClient
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.service import AgentService

FACTORY_TOOLS = ("get_machine_status", "list_open_work_orders", "get_safety_procedure")


@dataclass
class Runtime:
    store: SQLiteExecutionStore
    service: AgentService
    provider: LLMProvider
    executor: ToolExecutor
    mcp: SDKMCPClient | None


@asynccontextmanager
async def open_runtime(
    path: Path,
    provider: LLMProvider | None = None,
    *,
    executor: ToolExecutor | None = None,
    mcp_target: object | None = None,
    approved_tools: Sequence[str] = FACTORY_TOOLS,
    call_timeout: float = 2,
    max_attempts: int = 1,
) -> AsyncIterator[Runtime]:
    if executor is not None and mcp_target is not None:
        raise ValueError("Choose an executor or MCP target, not both")
    provider = FakeLLMProvider() if provider is None else provider
    async with AsyncExitStack() as stack:
        store = await stack.enter_async_context(SQLiteExecutionStore(path))
        mcp = None
        tools: tuple[ToolDefinition, ...] = ()
        if mcp_target is not None:
            mcp = await stack.enter_async_context(
                SDKMCPClient(mcp_target, startup_timeout_seconds=10)
            )
            executor = MCPToolExecutor(
                mcp,
                approved_tools,
                call_timeout_seconds=call_timeout,
                max_attempts=max_attempts,
            )
            tools = await executor.discover_tools()
        executor = FakeToolExecutor() if executor is None else executor
        service = await stack.enter_async_context(
            AgentService(AgentHarness(provider, executor), store, tools=tools)
        )
        yield Runtime(store, service, provider, executor, mcp)


async def seed(store: SQLiteExecutionStore, session_id: str = "s", run_id: str = "r") -> Run:
    await store.create_session(Session(session_id))
    run = Run(run_id, session_id, "queued", ExecutionLimits())
    message = Message("user", "check")
    await store.create_run(run, message, TaskState(objective=Fact("check", message.message_id)))
    return run


def rows(path: Path, query: str) -> list[Any]:
    with sqlite3.connect(path) as db:
        return db.execute(query).fetchall()


@contextmanager
def fail_record(store: SQLiteExecutionStore, record_type: str) -> Iterator[None]:
    original = store._event

    async def fail(db: Any, run: Any, kind: str, payload: Any) -> Any:
        if payload.get("record_type") == record_type:
            raise OSError("simulated storage write failure")
        return await original(db, run, kind, payload)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_event", fail)
        yield


@contextmanager
def lose_commit_ack(store: SQLiteExecutionStore) -> Iterator[None]:
    db = store._db
    if db is None:
        raise ValueError("Open the store before injecting a commit fault")
    commit = db.commit

    async def fail() -> None:
        await commit()
        raise OSError("commit completed but acknowledgement was lost")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "commit", fail)
        yield


class ProviderGate:
    """An on_generate callback: signal entry, then await release or cancellation."""

    def __init__(self, at_step: int = 1) -> None:
        self.at_step = at_step
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.cancelled = anyio.Event()

    async def __call__(self, step: int) -> None:
        if step != self.at_step:
            return
        self.entered.set()
        try:
            await self.release.wait()
        except anyio.get_cancelled_exc_class():
            self.cancelled.set()
            raise
