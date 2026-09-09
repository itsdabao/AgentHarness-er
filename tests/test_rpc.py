from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import StdioServerParameters

from fakes import FakeLLMProvider
from minder_harness.core import ModelResponse, ToolCall
from minder_harness.core.ports import LLMProvider
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.progress import progress_text
from minder_harness.providers.gemini import GeminiProvider
from minder_harness.rpc import create_app
from minder_harness.service import AgentService
from support import open_runtime

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def service_for(
    path: Path, provider: LLMProvider, *, real_mcp: bool = False
) -> AsyncIterator[AgentService]:
    server = (
        StdioServerParameters(command=sys.executable, args=["-m", "minder_mock_mcp.server"])
        if real_mcp
        else None
    )
    async with open_runtime(
        path, provider, mcp_target=server, approved_tools=["get_machine_status"]
    ) as runtime:
        yield runtime.service


async def rpc(http: httpx.AsyncClient, method: str, **params: Any) -> dict[str, Any]:
    response = await http.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        },
    )
    assert response.status_code == 200
    data: dict[str, Any] = response.json()
    assert data["id"] == 1
    return data


async def terminal(http: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    with anyio.fail_after(10):
        while True:
            result: dict[str, Any] = (await rpc(http, "get_run", run_id=run_id))["result"]["data"]
            if result["status"] not in {"queued", "running", "cancel_requested"}:
                return result
            await anyio.sleep(0.01)


async def test_rpc_actual_mcp_multistep_failure_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "rpc.db"
    provider = FakeLLMProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("one", "get_machine_status", {"machine_id": "CNC-04"}),)
            ),
            ModelResponse(content="warning"),
            ModelResponse(
                tool_calls=(ToolCall("two", "get_machine_status", {"machine_id": "missing"}),)
            ),
            ModelResponse(content="Cannot read this machine."),
        ]
    )
    app = create_app(lambda: service_for(path, provider, real_mcp=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as http:
            assert (await http.get("/health")).json()["data"]["ready"] is True
            session = (await rpc(http, "create_session"))["result"]["data"]["session_id"]
            run = (await rpc(http, "submit_task", session_id=session, content="check"))["result"][
                "data"
            ]
            assert run["status"] == "queued" and "events" not in run
            finished = await terminal(http, run["run_id"])
            assert finished["status"] == "completed" and finished["usage"]["tool_calls"] == 1
            cursor, seen = 0, []
            while True:
                page = (
                    await rpc(
                        http,
                        "list_run_events",
                        run_id=run["run_id"],
                        after_sequence=cursor,
                        limit=2,
                    )
                )["result"]["data"]
                seen.extend(page["events"])
                cursor = page["next_after_sequence"]
                if not page["has_more"]:
                    break
            assert [e["sequence"] for e in seen] == list(range(1, len(seen) + 1))
            assert seen[-1]["type"] == "run_completed"
            assert "_message" not in json.dumps(seen)
            failure_run = (
                await rpc(http, "submit_task", session_id=session, content="check missing")
            )["result"]["data"]
            assert (await terminal(http, failure_run["run_id"]))["status"] == "completed"
            failure_events = (await rpc(http, "list_run_events", run_id=failure_run["run_id"]))[
                "result"
            ]["data"]["events"]
            assert any(e["type"] == "tool_execution_failed" for e in failure_events)
    next_provider = FakeLLMProvider([ModelResponse(content="follow-up")])
    app2 = create_app(lambda: service_for(path, next_provider))
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app2), base_url="http://local"
        ) as http:
            assert (await rpc(http, "get_session", session_id=session))["result"]["data"][
                "message_count"
            ] > 0
            assert (await rpc(http, "get_run", run_id=run["run_id"]))["result"]["data"][
                "output"
            ] == "warning"
            new = (await rpc(http, "submit_task", session_id=session, content="next"))["result"][
                "data"
            ]
            assert new["run_id"] != run["run_id"]
            await terminal(http, new["run_id"])
            assert any(m.content == "warning" for m in next_provider.calls[0])
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "mcp-session"]


@pytest.mark.parametrize("retry", [False, True])
async def test_rpc_cancel_provider_io_and_retry_wait(tmp_path: Path, retry: bool) -> None:
    entered = anyio.Event()
    requests = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        entered.set()
        if retry:
            return httpx.Response(429, headers={"retry-after": "5"})
        await anyio.sleep(30)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
        provider = GeminiProvider(upstream, "SECRET_KEY")
        app = create_app(
            lambda: service_for(tmp_path / "cancel.db", provider), secrets=("SECRET_KEY",)
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://local"
            ) as http:
                sid = (await rpc(http, "create_session"))["result"]["data"]["session_id"]
                run = (await rpc(http, "submit_task", session_id=sid, content="hi"))["result"][
                    "data"
                ]
                await entered.wait()
                conflict = await rpc(http, "submit_task", session_id=sid, content="overlap")
                assert conflict["result"]["error"]["code"] == "CONFLICT"
                if retry:
                    with anyio.fail_after(2):
                        while True:
                            events = (await rpc(http, "list_run_events", run_id=run["run_id"]))[
                                "result"
                            ]["data"]["events"]
                            if any(e["payload"].get("retry_scheduled") for e in events):
                                break
                            await anyio.sleep(0.01)
                await rpc(http, "cancel_run", run_id=run["run_id"])
                cancelled = await terminal(http, run["run_id"])
                assert cancelled["status"] == "cancelled" and cancelled["output"] is None
                assert cancelled["usage"]["model_steps"] == 1
                assert requests == 1
                before = await rpc(http, "list_run_events", run_id=run["run_id"])
                await rpc(http, "cancel_run", run_id=run["run_id"])
                assert await rpc(http, "list_run_events", run_id=run["run_id"]) == before


async def test_protocol_validation_redaction_and_cancel_completed(tmp_path: Path) -> None:
    provider = FakeLLMProvider([ModelResponse(content="SECRET_KEY done")])
    app = create_app(
        lambda: service_for(tmp_path / "validation.db", provider), secrets=("SECRET_KEY",)
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as http:
            assert (await http.post("/rpc", content="{")).json()["error"]["code"] == -32700
            assert (await http.post("/rpc", json=[])).json()["error"]["code"] == -32600
            assert (await rpc(http, "missing"))["error"]["code"] == -32601
            assert (await rpc(http, "get_run"))["error"]["code"] == -32602
            assert (await rpc(http, "get_run", run_id="missing"))["result"]["error"][
                "code"
            ] == "NOT_FOUND"
            assert (await http.post("/rpc", content=" " * 65537)).status_code == 413
            notification = await http.post(
                "/rpc", json={"jsonrpc": "2.0", "method": "create_session"}
            )
            assert notification.status_code == 204 and not notification.content
            sid = (await rpc(http, "create_session"))["result"]["data"]["session_id"]
            invalid = await rpc(
                http, "submit_task", session_id=sid, content="hi", limits={"max_steps": 0}
            )
            assert invalid["error"]["code"] == -32602 and provider.calls == []
            run = (await rpc(http, "submit_task", session_id=sid, content="hi"))["result"]["data"]
            result = await terminal(http, run["run_id"])
            assert result["output"] == "[REDACTED] done"
            assert (await rpc(http, "cancel_run", run_id=run["run_id"]))["result"]["data"][
                "status"
            ] == "completed"
            events = await rpc(http, "list_run_events", run_id=run["run_id"])
            assert "SECRET_KEY" not in json.dumps(events)


async def test_shutdown_cancels_and_persists_active_run(tmp_path: Path) -> None:
    entered = anyio.Event()

    class SlowProvider(FakeLLMProvider):
        async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
            entered.set()
            await anyio.sleep(30)
            return ModelResponse(content="late")

    path = tmp_path / "shutdown.db"
    app = create_app(lambda: service_for(path, SlowProvider()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as http:
            sid = (await rpc(http, "create_session"))["result"]["data"]["session_id"]
            run = (await rpc(http, "submit_task", session_id=sid, content="hi"))["result"]["data"]
            await entered.wait()
    async with SQLiteExecutionStore(path) as store:
        assert (await store.get_run(run["run_id"])).status == "cancelled"
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("agent:")]


def test_progress_is_fixed_and_only_announces_recorded_retry() -> None:
    event: dict[str, Any] = {
        "type": "model_response_received",
        "payload": {
            "error": {"code": "PROVIDER_RATE_LIMITED", "retryable": True},
        },
    }
    assert "thử lại" not in progress_text(event)
    event["payload"].update(
        {
            "retry_reason": "PROVIDER_RATE_LIMITED",
            "retry_delay_ms": 2000,
            "attempt": 1,
            "max_attempts": 2,
        }
    )
    assert "2000 ms" in progress_text(event) and "2/2" in progress_text(event)
