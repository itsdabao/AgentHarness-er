from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import anyio
import pytest
from mcp import StdioServerParameters

import minder_harness.persistence.sqlite as persistence
from fakes import FakeLLMProvider, FakeToolExecutor
from minder_harness.core import (
    ExecutionLimits,
    Message,
    ModelResponse,
    Run,
    Session,
    ToolCall,
    ToolResult,
)
from minder_harness.core.context import TaskState
from minder_harness.core.models import as_jsonable
from minder_harness.core.ports import StorageError
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.persistence.ownership import DatabaseOwnership
from minder_harness.persistence.schema import MIGRATIONS
from support import ProviderGate, fail_record, open_runtime, rows, seed

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("reopen", [False, True], ids=["DB01", "DB02"])
async def test_failed_migration_is_atomic_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reopen: bool
) -> None:
    path = tmp_path / "historical.db"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                db.execute(statement)
        db.execute(
            "INSERT INTO sessions VALUES (?, ?)", ("old", json.dumps(as_jsonable(Session("old"))))
        )
        db.execute("PRAGMA user_version=2")
    before = rows(path, "SELECT name, sql FROM sqlite_master ORDER BY name")
    broken = MIGRATIONS[:2] + (
        MIGRATIONS[2]
        + ("CREATE TABLE partial_upgrade (id INTEGER)", "INSERT INTO absent VALUES (1)"),
    )
    with monkeypatch.context() as patch:
        patch.setattr(persistence, "MIGRATIONS", broken)
        with pytest.raises(StorageError):
            async with SQLiteExecutionStore(path):
                pytest.fail("Migration must fail")
    assert rows(path, "PRAGMA user_version") == [(2,)]
    assert rows(path, "SELECT name, sql FROM sqlite_master ORDER BY name") == before
    assert rows(path, "SELECT id FROM sessions") == [("old",)]
    # Prove failure released ownership even when this test does not retry migration.
    owner = DatabaseOwnership(path)
    owner.acquire()
    owner.release()
    if reopen:
        async with SQLiteExecutionStore(path) as store:
            assert (await store.get_session("old")).session_id == "old"
        assert rows(path, "PRAGMA user_version") == [(len(MIGRATIONS),)]


async def test_write_lock_released_before_timeout(tmp_path: Path) -> None:
    path = tmp_path / "lock.db"
    loop = asyncio.get_running_loop()
    attempted = asyncio.Event()
    with anyio.fail_after(5):
        async with SQLiteExecutionStore(path) as store:
            assert store._db is not None
            await store._db.execute("PRAGMA busy_timeout=2000")
            await store._db.set_trace_callback(
                lambda sql: (
                    loop.call_soon_threadsafe(attempted.set) if sql == "BEGIN IMMEDIATE" else None
                )
            )
            with sqlite3.connect(path, isolation_level=None) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                task = asyncio.create_task(store.create_session(Session("after-lock")))
                try:
                    await attempted.wait()
                    assert not task.done()
                finally:
                    blocker.rollback()
                    await task
            assert (await store.get_session("after-lock")).session_id == "after-lock"
    assert rows(path, "SELECT id FROM sessions") == [("after-lock",)]


async def test_write_lock_timeout_quarantines_without_partial_write(tmp_path: Path) -> None:
    path = tmp_path / "busy.db"
    with anyio.fail_after(5):
        async with SQLiteExecutionStore(path) as store:
            assert store._db is not None
            await store._db.execute("PRAGMA busy_timeout=25")
            with sqlite3.connect(path, isolation_level=None) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(StorageError) as failure:
                        await store.create_session(Session("not-written"))
                    assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
                    assert failure.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
                finally:
                    blocker.rollback()
            with pytest.raises(StorageError, match="quarantined"):
                await store.create_session(Session("still-refused"))
    assert rows(path, "SELECT id FROM sessions") == []


@pytest.mark.parametrize("body_error", [False, True], ids=["DB05", "DB06"])
async def test_runtime_cleanup(tmp_path: Path, body_error: bool) -> None:
    path = tmp_path / "cleanup.db"
    target = StdioServerParameters(command=sys.executable, args=["-m", "minder_mock_mcp.server"])
    gate = ProviderGate()
    provider = FakeLLMProvider(always=ModelResponse(content="late"), on_generate=gate)
    before = set(asyncio.all_tasks())
    with anyio.fail_after(15):
        try:
            async with open_runtime(path, provider, mcp_target=target) as runtime:
                session = await runtime.service.create_session()
                run = await runtime.service.submit_task(session.session_id, "wait")
                await gate.entered.wait()
                assert runtime.mcp is not None and runtime.mcp.is_ready
                if body_error:
                    raise ValueError("intentional body failure")
        except ValueError as exc:
            assert body_error and str(exc) == "intentional body failure"
        assert gate.cancelled.is_set()
        assert runtime.store._db is None and runtime.store._owner.stream is None
        assert runtime.mcp is not None and not runtime.mcp.is_ready
        assert runtime.mcp._owner is None and not runtime.service._tasks
        assert not {task for task in asyncio.all_tasks() - before if not task.done()}
        async with SQLiteExecutionStore(path) as store:
            assert (await store.get_run(run.run_id)).status == "cancelled"


async def test_partial_setup_unwinds_open_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from minder_harness.mcp import MCPToolExecutor, SDKMCPClient

    path = tmp_path / "partial.db"
    opened: list[SDKMCPClient] = []
    target = StdioServerParameters(command=sys.executable, args=["-m", "minder_mock_mcp.server"])

    async def fail_discovery(executor: MCPToolExecutor, **kwargs: object) -> tuple[()]:
        client = executor._client
        assert isinstance(client, SDKMCPClient) and client.is_ready
        opened.append(client)
        raise ValueError("fixture discovery failure")

    before = set(asyncio.all_tasks())
    with anyio.fail_after(15):
        with monkeypatch.context() as patch:
            patch.setattr(MCPToolExecutor, "discover_tools", fail_discovery)
            with pytest.raises(ValueError, match="fixture discovery"):
                async with open_runtime(path, mcp_target=target):
                    pytest.fail("Setup should fail")
        assert len(opened) == 1 and not opened[0].is_ready and opened[0]._owner is None
        assert not {task for task in asyncio.all_tasks() - before if not task.done()}
        async with SQLiteExecutionStore(path):
            pass


async def test_runtime_state_and_fake_histories_are_isolated(tmp_path: Path) -> None:
    call = ToolCall("only-a", "fixture_read", {})
    provider = FakeLLMProvider([ModelResponse(tool_calls=(call,)), ModelResponse(content="done")])
    executor = FakeToolExecutor({call.tool_call_id: ToolResult.success(call, {"value": "only A"})})
    async with (
        open_runtime(tmp_path / "a.db", provider, executor=executor) as a,
        open_runtime(tmp_path / "b.db") as b,
    ):
        assert a.provider is not b.provider and a.executor is not b.executor
        assert isinstance(a.provider, FakeLLMProvider) and isinstance(b.provider, FakeLLMProvider)
        assert isinstance(a.executor, FakeToolExecutor) and isinstance(b.executor, FakeToolExecutor)
        session = await a.service.create_session()
        with pytest.raises(KeyError):
            await b.store.get_session(session.session_id)
        run = await a.service.submit_task(session.session_id, "only A")
        assert (await a.service.wait_run(run.run_id)).output == "done"
        assert len(a.provider.calls) == 2 and len(a.executor.calls) == 1
        assert b.provider.calls == [] and b.executor.calls == []
        assert await b.store.list_sessions() == {
            "sessions": [],
            "next_before": None,
            "has_more": False,
        }
        assert rows(b.store.path, "SELECT COUNT(*) FROM runs") == [(0,)]
        assert rows(b.store.path, "SELECT COUNT(*) FROM events") == [(0,)]


async def test_fault_patch_is_restored_after_exception(tmp_path: Path) -> None:
    path = tmp_path / "restore.db"
    async with SQLiteExecutionStore(path) as store:
        run = await seed(store)
        original = store._event
        with pytest.raises(StorageError):
            with fail_record(store, "probe"):
                await store.record_event(run, "run_started", {"record_type": "probe"})
        assert store._event == original
    async with open_runtime(
        tmp_path / "fresh.db", FakeLLMProvider([ModelResponse(content="ok")])
    ) as runtime:
        session = await runtime.service.create_session()
        run = await runtime.service.submit_task(session.session_id, "works")
        assert (await runtime.service.wait_run(run.run_id)).output == "ok"


async def test_domain_conflict_leaves_store_usable(tmp_path: Path) -> None:
    path = tmp_path / "domain.db"
    async with SQLiteExecutionStore(path) as store:
        await seed(store)
        before = await store.list_run_events("r")
        with pytest.raises(ValueError, match="already active"):
            await store.create_run(
                Run("other", "s", "queued", ExecutionLimits()),
                Message("user", "overlap"),
                TaskState(),
            )
        assert await store.list_run_events("r") == before
        assert rows(path, "SELECT id FROM runs") == [("r",)]
        assert rows(path, "SELECT COUNT(*) FROM messages") == [(1,)]
        await store.create_session(Session("valid"))
        assert (await store.get_session("valid")).session_id == "valid"
