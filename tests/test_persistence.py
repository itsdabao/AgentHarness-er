from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest

from fakes import FakeLLMProvider, FakeToolExecutor
from minder_harness.core import (
    AgentHarness,
    ExecutionLimits,
    Message,
    ModelResponse,
    Run,
    Session,
    ToolCall,
)
from minder_harness.core.context import TaskState
from minder_harness.core.models import as_jsonable
from minder_harness.core.ports import StorageError
from minder_harness.mcp import MCPToolExecutor, MCPToolResponse
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.service import AgentService
from support import ProviderGate, fail_record, lose_commit_ack, open_runtime, rows, seed
from test_mcp import STATUS_TOOL, UPDATE_TOOL, FakeMCPClient

pytestmark = pytest.mark.anyio


async def test_durable_multi_step_restart_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    call = ToolCall("same_provider_id", "get_machine_status", {"machine_id": "CNC-04"})
    client = FakeMCPClient([STATUS_TOOL], [MCPToolResponse({"status": "warning"})])
    executor = MCPToolExecutor(client, ["get_machine_status"], server_scope="factory/read-only")
    provider = FakeLLMProvider(
        [ModelResponse(tool_calls=(call,)), ModelResponse(content="warning")]
    )
    async with SQLiteExecutionStore(path) as store:
        async with AgentService(AgentHarness(provider, executor), store) as service:
            session = await service.create_session()
            queued = await service.submit_task(
                session.session_id, "check", constraints=["Read only"]
            )
            assert queued.status == "queued"
            run = await service.wait_run(queued.run_id)
            assert run.status == "completed" and run.output == "warning"
            assert run.events == [] and run.usage.tool_calls == 1
            page = await service.list_run_events(run.run_id, limit=2)
            assert page["has_more"] is True
            assert page["next_after_sequence"] == 2
            state = await store.get_task_state(session.session_id)
            assert state.constraints[0].value == "Read only"
            assert state.observations[0].value == {"status": "warning"}
            assert state.observations[0].observed_at
    assert rows(path, "SELECT state FROM attempts") == [("succeeded",)]
    async with SQLiteExecutionStore(path) as store:
        provider2 = FakeLLMProvider([ModelResponse(content="follow-up")])
        async with AgentService(AgentHarness(provider2, FakeToolExecutor()), store) as service:
            previous = await service.get_run(run.run_id)
            assert previous.output == "warning"
            followup = await service.submit_task(session.session_id, "what next?")
            assert (await service.wait_run(followup.run_id)).status == "completed"
            assert any(message.content == "warning" for message in provider2.calls[0])
            assert (await store.get_task_state(session.session_id)).constraints == state.constraints
            events = await store.list_run_events(run.run_id)
            data = events["events"]
            assert isinstance(data, list)
            assert [e["sequence"] for e in data if isinstance(e, dict)] == list(
                range(1, len(data) + 1)
            )
            assert (await store.get_session(session.session_id)).messages[-1].content == "follow-up"


@pytest.mark.parametrize(
    "status, expected",
    [
        ("queued", "interrupted"),
        ("running", "interrupted"),
        ("cancel_requested", "cancelled"),
    ],
)
async def test_recovery_states_and_no_replay(tmp_path: Path, status: str, expected: str) -> None:
    path = tmp_path / "recover.db"
    async with SQLiteExecutionStore(path) as store:
        run = await seed(store)
        if status != "queued":
            await store.record_event(run, "run_started", {})
        if status == "cancel_requested":
            await store.cancel_run(run.run_id)
    async with SQLiteExecutionStore(path) as store:
        await store.recover()
        assert (await store.get_run("r")).status == expected
        before = await store.list_run_events("r")
        await store.recover()
        assert await store.list_run_events("r") == before


async def test_cancel_vs_completion_is_atomic_and_idempotent(tmp_path: Path) -> None:
    async with SQLiteExecutionStore(tmp_path / "race.db") as store:
        run = await seed(store)
        await store.record_event(run, "run_started", {})
        await store.cancel_run(run.run_id)
        run.status = "completed"
        run.output = "late"
        await store.record_event(run, "run_completed", {"output": "late"})
        result = await store.get_run(run.run_id)
        assert result.status == "cancelled" and result.output is None
        before = await store.list_run_events(run.run_id)
        await store.cancel_run(run.run_id)
        assert await store.list_run_events(run.run_id) == before


async def test_queued_cancel_does_not_start_provider(tmp_path: Path) -> None:
    provider = FakeLLMProvider([ModelResponse(content="must not run")])
    async with SQLiteExecutionStore(tmp_path / "queued.db") as store:
        run = await seed(store)
        cancelled = await store.cancel_run(run.run_id)
        assert cancelled.status == "cancelled"
        async with AgentService(AgentHarness(provider, FakeToolExecutor()), store) as service:
            assert (await service.get_run(run.run_id)).status == "cancelled"
            assert provider.calls == []


async def test_live_cancel_and_session_exclusion(tmp_path: Path) -> None:
    gate = ProviderGate()
    provider = FakeLLMProvider(always=ModelResponse(content="late"), on_generate=gate)
    with anyio.fail_after(5):
        async with open_runtime(tmp_path / "live.db", provider) as runtime:
            service, store = runtime.service, runtime.store
            session = await service.create_session()
            run = await service.submit_task(session.session_id, "first")
            await gate.entered.wait()
            with pytest.raises(ValueError, match="already active"):
                await service.submit_task(session.session_id, "overlap")
            await service.cancel_run(run.run_id)
            with anyio.fail_after(2):
                assert (await service.wait_run(run.run_id)).status == "cancelled"
            assert not any(
                m.content == "late" for m in (await store.get_session(session.session_id)).messages
            )
            assert gate.cancelled.is_set()


async def test_unsafe_guard_survives_new_executor_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "guard.db"
    async with SQLiteExecutionStore(path) as store:
        run = await seed(store)
        await store.record_event(run, "run_started", {})
        call = ToolCall("provider", "update_machine_mode", {"mode": "idle"}, "r", "execution")
        attempt_id = await store.begin_attempt(call, "factory/operator", 1, False)
    async with SQLiteExecutionStore(path) as store:
        await store.recover()
        client = FakeMCPClient([UPDATE_TOOL], [MCPToolResponse({"mode": "idle"})])
        executor = MCPToolExecutor(client, ["update_machine_mode"], server_scope="factory/operator")
        provider = FakeLLMProvider(
            [
                ModelResponse(tool_calls=(replace(call, tool_call_id="new_provider_id"),)),
                ModelResponse(content="needs verification"),
            ]
        )
        async with AgentService(AgentHarness(provider, executor), store) as service:
            resumed = await service.submit_task("s", "try again")
            assert (await service.wait_run(resumed.run_id)).status == "completed"
            assert client.calls == []
            assert any(
                isinstance(m.content, dict)
                and m.content.get("error_code") == "AMBIGUOUS_TOOL_OUTCOME"
                for m in provider.calls[-1]
            )
        await store.resolve_attempt(attempt_id, "Operator verified no change occurred")
        # Explicit reconciliation is recorded, never an agent tool or automatic recovery action.
        assert rows(path, "SELECT outcome_unknown FROM attempts")[0][0] == 0


async def test_storage_failure_rolls_back_intent_and_prevents_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "failure.db"
    client = FakeMCPClient([STATUS_TOOL], [MCPToolResponse({})])
    provider = FakeLLMProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("one", "get_machine_status", {"machine_id": "CNC-04"}),)
            )
        ]
    )
    async with SQLiteExecutionStore(path) as store:
        with pytest.raises(StorageError), fail_record(store, "attempt_intent"):
            async with AgentService(
                AgentHarness(
                    provider,
                    MCPToolExecutor(client, ["get_machine_status"]),
                ),
                store,
            ) as service:
                session = await service.create_session()
                run = await service.submit_task(session.session_id, "check")
                await service.wait_run(run.run_id)
        assert client.calls == []
    assert rows(path, "SELECT COUNT(*) FROM attempts") == [(0,)]


async def test_second_runtime_cannot_open_owned_database(tmp_path: Path) -> None:
    path = tmp_path / "owned.db"
    async with SQLiteExecutionStore(path):
        script = (
            "import anyio; from minder_harness.persistence import SQLiteExecutionStore; "
            "exec('async def main():\\n async with SQLiteExecutionStore(' + repr("
            + repr(str(path))
            + ") + '): pass'); anyio.run(main)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        assert completed.returncode != 0
        assert "already owned" in completed.stderr
    async with SQLiteExecutionStore(path):
        pass


async def test_future_schema_rejected_and_v1_upgraded_without_data_loss(tmp_path: Path) -> None:
    from minder_harness.persistence.schema import MIGRATIONS

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        for statement in MIGRATIONS[0]:
            db.execute(statement)
        db.execute(
            "INSERT INTO sessions VALUES(?,?)", ("old", json.dumps(as_jsonable(Session("old"))))
        )
        db.execute("PRAGMA user_version=1")
    async with SQLiteExecutionStore(path) as store:
        assert (await store.get_session("old")).session_id == "old"
        assert (await store.get_task_state("old")).schema_version == 1
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="future"):
        async with SQLiteExecutionStore(path):
            pass
    assert rows(path, "PRAGMA user_version") == [(999,)]


@pytest.mark.parametrize(
    "mode, count, unknown",
    [
        ("before_intent", 0, False),
        ("after_intent", 1, True),
        ("after_remote", 1, True),
        ("after_result", 1, False),
    ],
)
async def test_actual_process_crash_windows(
    tmp_path: Path,
    mode: str,
    count: int,
    unknown: bool,
) -> None:
    from minder_harness.core.ports import ReplayBlocked

    path = tmp_path / "crashed.db"
    fixture = Path(__file__).parent / "fixtures" / "persistence_crash.py"
    completed = subprocess.run(
        [sys.executable, str(fixture), str(path), mode],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 17, completed.stderr
    assert rows(path, "SELECT COUNT(*) FROM attempts") == [(count,)]
    assert path.with_suffix(".effect").exists() == (mode in {"after_remote", "after_result"})
    async with SQLiteExecutionStore(path) as store:
        await store.recover()
        assert (await store.get_run("run")).status == "interrupted"
        outcomes = await store.execution_outcomes("session")
        if unknown:
            assert outcomes["execution"] == "outcome_unknown"
        resumed = Run("new-run", "session", "queued", ExecutionLimits())
        await store.create_run(resumed, Message("user", "again"), TaskState())
        await store.record_event(resumed, "run_started", {})
        call = ToolCall("provider-id", "update", {"mode": "idle"}, "new-run", "new-execution")
        if unknown:
            with pytest.raises(ReplayBlocked):
                await store.begin_attempt(call, "server/operator", 1, False)
        else:
            await store.begin_attempt(call, "server/operator", 1, False)


async def test_retry_attempts_persist_without_multiplying_budget(tmp_path: Path) -> None:
    path = tmp_path / "attempts.db"
    client = FakeMCPClient([STATUS_TOOL], [TimeoutError("slow"), MCPToolResponse({"ok": True})])
    provider = FakeLLMProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("p", "get_machine_status", {"machine_id": "CNC-04"}),)
            ),
            ModelResponse(content="done"),
        ]
    )
    async with SQLiteExecutionStore(path) as store:
        executor = MCPToolExecutor(client, ["get_machine_status"], retry_delay_seconds=0)
        async with AgentService(AgentHarness(provider, executor), store) as service:
            session = await service.create_session()
            run = await service.submit_task(session.session_id, "check")
            finished = await service.wait_run(run.run_id)
            assert finished.status == "completed"
            assert finished.usage.tool_calls == 1 and len(client.calls) == 2
    assert rows(path, "SELECT attempt,state FROM attempts ORDER BY attempt") == [
        (1, "failed"),
        (2, "succeeded"),
    ]


async def test_failed_result_commit_never_replays_remote_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result-fail.db"
    client = FakeMCPClient([UPDATE_TOOL], [MCPToolResponse({"ok": True})])
    provider = FakeLLMProvider(
        [
            ModelResponse(tool_calls=(ToolCall("p", "update_machine_mode", {"mode": "idle"}),)),
            ModelResponse(content="must not continue"),
        ]
    )
    async with SQLiteExecutionStore(path) as store:
        with pytest.raises(StorageError), fail_record(store, "attempt_result"):
            async with AgentService(
                AgentHarness(provider, MCPToolExecutor(client, ["update_machine_mode"])),
                store,
            ) as service:
                session = await service.create_session()
                run = await service.submit_task(session.session_id, "update")
                await service.wait_run(run.run_id)
    assert len(client.calls) == 1 and len(provider.calls) == 1
    assert rows(path, "SELECT state,outcome_unknown FROM attempts") == [("pending", 1)]


async def test_context_overflow_fails_before_provider_dispatch(tmp_path: Path) -> None:
    provider = FakeLLMProvider([ModelResponse(content="should not run")])
    async with SQLiteExecutionStore(tmp_path / "context.db") as store:
        async with AgentService(
            AgentHarness(provider, FakeToolExecutor()),
            store,
            context_limit=100,
        ) as service:
            session = await service.create_session()
            run = await service.submit_task(session.session_id, "required input " * 50)
            result = await service.wait_run(run.run_id)
            assert result.status == "failed"
            assert result.error is not None and result.error.code == "CONTEXT_BUDGET_EXCEEDED"
            assert provider.calls == []


async def test_shutdown_observes_tasks_and_refuses_new_admission(tmp_path: Path) -> None:
    gate = ProviderGate()
    provider = FakeLLMProvider(always=ModelResponse(content="late"), on_generate=gate)
    with anyio.fail_after(5):
        async with open_runtime(tmp_path / "shutdown.db", provider) as runtime:
            service = runtime.service
            session = await service.create_session()
            run = await service.submit_task(session.session_id, "check")
            await gate.entered.wait()
            with anyio.fail_after(2):
                await service.shutdown()
            assert (await service.get_run(run.run_id)).status == "cancelled"
            assert not [
                task for task in asyncio.all_tasks() if task.get_name().startswith("agent:")
            ]
            with pytest.raises(ValueError, match="not accepting"):
                await service.submit_task(session.session_id, "again")
            assert gate.cancelled.is_set()


async def test_real_mcp_server_with_durable_supervisor(tmp_path: Path) -> None:
    from minder_harness.mcp import SDKMCPClient
    from minder_mock_mcp.server import build_server

    path = tmp_path / "real.db"
    provider = FakeLLMProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("real", "get_machine_status", {"machine_id": "CNC-04"}),)
            ),
            ModelResponse(content="warning"),
        ]
    )
    async with SQLiteExecutionStore(path) as store, SDKMCPClient(build_server()) as client:
        executor = MCPToolExecutor(client, ["get_machine_status"], server_scope="mock/read-only")
        tools = await executor.discover_tools()
        async with AgentService(AgentHarness(provider, executor), store, tools=tools) as service:
            session = await service.create_session()
            run = await service.submit_task(session.session_id, "check CNC-04")
            finished = await service.wait_run(run.run_id)
            assert finished.output == "warning"
            assert provider.tool_definitions[0] == tools
    assert rows(path, "SELECT state FROM attempts") == [("succeeded",)]


async def test_cancelled_final_answer_is_audit_only(tmp_path: Path) -> None:
    async with SQLiteExecutionStore(tmp_path / "late.db") as store:
        run = await seed(store)
        await store.record_event(run, "run_started", {})
        answer = Message("assistant", "late answer")
        await store.record_event(run, "model_response_received", {"_message": as_jsonable(answer)})
        await store.cancel_run(run.run_id)
        run.status = "completed"
        run.output = "late answer"
        await store.record_event(run, "run_completed", {"output": run.output})
        assert not any(m.content == "late answer" for m in (await store.get_session("s")).messages)
        events = await store.list_run_events("r")
        assert "late answer" in json.dumps(events)  # Original trace is still available.


async def test_deadline_during_event_recording_rejects_final_output() -> None:
    from minder_harness.core import InMemoryEventCollector

    collector = InMemoryEventCollector("s", "r")

    async def slow_record(kind: str, payload: Any) -> Any:
        if kind == "model_response_received":
            await anyio.sleep(0.04)
        return await collector.emit(kind, payload)

    run = await AgentHarness(
        FakeLLMProvider([ModelResponse(content="late")]),
        FakeToolExecutor(),
    ).run(Session("s"), "hi", limits=ExecutionLimits(timeout_seconds=0.02), event_sink=slow_record)
    assert run.status == "limit_exceeded"
    assert run.output is None


async def test_commit_ack_loss_quarantines_store_without_partial_transition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "commit.db"
    async with SQLiteExecutionStore(path) as store:
        run = await seed(store)
        await store.record_event(run, "run_started", {})
        run.status = "completed"
        run.output = "done"
        with pytest.raises(StorageError), lose_commit_ack(store):
            await store.record_event(run, "run_completed", {"output": "done"})
        with pytest.raises(StorageError, match="quarantined"):
            await store.get_run(run.run_id)
    async with SQLiteExecutionStore(path) as store:
        await store.recover()
        assert (await store.get_run("r")).status == "completed"
        events = await store.list_run_events("r")
        assert "run_completed" in json.dumps(events)


async def test_cancel_real_inflight_tool_persists_unknown_and_repairs_resume(
    tmp_path: Path,
) -> None:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    from minder_harness.mcp import SDKMCPClient

    entered = anyio.Event()
    server = MCPServer("slow-write")

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    async def update() -> dict[str, bool]:
        entered.set()
        await anyio.sleep(30)
        return {"done": True}

    path = tmp_path / "cancel-tool.db"
    provider = FakeLLMProvider([ModelResponse(tool_calls=(ToolCall("write", "update", {}),))])
    with anyio.fail_after(5):
        async with SQLiteExecutionStore(path) as store, SDKMCPClient(server) as client:
            executor = MCPToolExecutor(client, ["update"], server_scope="test/operator")
            async with AgentService(AgentHarness(provider, executor), store) as service:
                session = await service.create_session()
                run = await service.submit_task(session.session_id, "update")
                await entered.wait()
                await service.cancel_run(run.run_id)
                assert (await service.wait_run(run.run_id)).status == "cancelled"
                provider2 = FakeLLMProvider([ModelResponse(content="Need verification")])
                service.harness.provider = provider2
                followup = await service.submit_task(session.session_id, "What happened?")
                await service.wait_run(followup.run_id)
                assert any(
                    isinstance(m.content, dict)
                    and m.content.get("execution_state") == "outcome_unknown"
                    for m in provider2.calls[0]
                )
    assert rows(path, "SELECT outcome_unknown FROM attempts") == [(1,)]
