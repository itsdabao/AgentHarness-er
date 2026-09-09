from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from fakes import FakeLLMProvider
from minder_harness.cli import TerminalClient, safe_text
from minder_harness.core import ModelResponse, ToolCall
from minder_harness.observation import accept_page, watch_run
from minder_harness.rpc import create_app
from minder_harness.rpc_client import RPCClient
from test_rpc import service_for


def event(sequence: int) -> dict[str, Any]:
    return {"sequence": sequence, "type": "run_started", "payload": {}}


def test_accept_pages_deduplicates_and_refuses_stalled_cursor() -> None:
    values, cursor = accept_page({"events": [event(2), event(1), event(2)], "has_more": False}, 1)
    assert values == [event(2)] and cursor == 2
    with pytest.raises(ValueError, match="no progress"):
        accept_page({"events": [event(2)], "has_more": True}, 2)


def test_terminal_controls_are_escaped_and_truncation_is_explicit() -> None:
    assert "\x1b" not in safe_text("\x1b[2Jsecret\r\x07")
    assert "\\u001b" in safe_text("\x1b[2J")
    assert "truncated" in safe_text("x" * 100, limit=10)


@pytest.mark.anyio
async def test_watch_drains_terminal_pages_before_return() -> None:
    rpc = AsyncMock(spec=RPCClient)
    rpc.call.side_effect = [
        {"run_id": "r", "status": "completed"},
        {"events": [event(1)], "has_more": True},
        {"events": [event(1), event(2)], "has_more": False},
    ]
    received: list[tuple[str, dict[str, Any]]] = []
    cursors: dict[str, int] = {}
    await watch_run(rpc, "r", cursors, lambda k, d: received.append((k, d)))
    assert [d["sequence"] for k, d in received if k == "event"] == [1, 2]
    assert received[-1][0] == "terminal" and cursors == {"r": 2}


@pytest.mark.anyio
async def test_cancel_is_independent_of_blocked_poll_and_quit_sends_no_cancel() -> None:
    blocked = asyncio.Event()
    methods: list[str] = []

    async def call(method: str, **params: Any) -> dict[str, Any]:
        methods.append(method)
        if method == "get_run":
            blocked.set()
            await asyncio.Event().wait()
        return {"run_id": "r", "status": "cancel_requested"}

    rpc = AsyncMock(spec=RPCClient)
    rpc.call.side_effect = call
    console = TerminalClient(rpc, json_mode=True)
    await console.watch("r")
    await asyncio.wait_for(blocked.wait(), 1)
    await asyncio.wait_for(console.execute("run cancel"), 1)
    assert methods == ["get_run", "cancel_run"]
    assert await console.execute("quit") is False
    assert methods.count("cancel_run") == 1 and console.watching is None


@pytest.mark.anyio
async def test_lost_submit_response_never_resubmits(capsys: pytest.CaptureFixture[str]) -> None:
    calls = 0

    async def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("sensitive details", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="http://test"
    ) as http:
        console = TerminalClient(RPCClient(http), session="s", json_mode=True)
        with pytest.raises(httpx.ReadTimeout) as failure:
            await console.execute("task hello")
        console.error("task", failure.value)
        assert console.run_id is None
    assert calls == 1
    output = json.loads(capsys.readouterr().out)
    assert "unconfirmed" in output["data"]["message"]
    assert "sensitive" not in str(output)


@pytest.mark.anyio
async def test_cli_real_mcp_and_json_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeLLMProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("c", "get_machine_status", {"machine_id": "CNC-04"}),)
            ),
            ModelResponse(content="warning"),
        ]
    )
    app = create_app(lambda: service_for(tmp_path / "cli.db", provider, real_mcp=True))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            console = TerminalClient(RPCClient(http), json_mode=True, interval=0.01)
            await console.execute("session new")
            await console.execute("task Check CNC-04")
            assert console.watching is not None
            await asyncio.wait_for(console.watching, 10)
            assert console.last_state is not None and console.last_state["status"] == "completed"
            run_id = console.run_id
            cursor = dict(console.cursors)
            await console.execute(f"run events {run_id}")
            assert console.cursors == cursor
            await console.execute(f"run show {run_id}")
            await console.execute("watch stop")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(r["kind"] == "event_page" for r in records)
    assert any(r["kind"] == "terminal" and r["data"]["output"] == "warning" for r in records)


@pytest.mark.anyio
async def test_web_assets_and_same_origin_protection(tmp_path: Path) -> None:
    app = create_app(lambda: service_for(tmp_path / "web.db", FakeLLMProvider()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            page = await http.get("/")
            assert page.status_code == 200
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            assert "Đơn giản" in page.text and "Chi tiết" in page.text
            assert 'id="session-toggle"' in page.text
            assert 'id="session-id"' not in page.text
            for path in ("app.js", "client.js", "pipeline.js", "sessions.js", "style.css"):
                asset = await http.get("/ui-assets/" + path)
                assert asset.status_code == 200
            assert (await http.get("/ui-assets/.env")).status_code == 404
            assert (await http.get("/ui-assets/%2e%2e/app.py")).status_code == 404
            body = {"jsonrpc": "2.0", "id": 1, "method": "create_session"}
            assert (
                await http.post("/rpc", json=body, headers={"origin": "https://other"})
            ).status_code == 403
            assert (await http.post("/rpc", json=body, headers={"origin": "http://test"})).json()[
                "result"
            ]["ok"]


def test_browser_client_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed only for browser-JS contract tests")
    result = subprocess.run(
        [node, "--test", "tests/web_client.test.mjs"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
