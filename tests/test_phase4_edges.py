from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server import MCPServer

import minder_harness.app as application
from fakes import FakeToolExecutor
from minder_harness.core import AgentHarness, Message, Session
from minder_harness.core.ports import ProviderError
from minder_harness.mcp.client import SDKMCPClient
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.providers.gemini import GeminiProvider
from test_gemini import answer

pytestmark = pytest.mark.anyio


async def test_retry_info_and_disabled_retry() -> None:
    for max_attempts, expected in ((1, 1), (2, 2)):
        calls = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                    "retryDelay": "0s",
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(200, json=answer())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            provider = GeminiProvider(http, "secret", max_attempts=max_attempts)
            if max_attempts == 1:
                with pytest.raises(ProviderError):
                    await provider.generate([Message("user", "hi")])
            else:
                assert (await provider.generate([Message("user", "hi")])).content == "done"
        assert calls == expected


async def test_startup_failure_releases_database_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        application,
        "SDKMCPClient",
        lambda *args, **kwargs: SDKMCPClient(MCPServer("missing-required-tools")),
    )
    path = tmp_path / "startup.db"
    with pytest.raises(ValueError, match="Required MCP"):
        async with application.runtime(application.Settings(database=str(path)), "fake-key"):
            pytest.fail("Startup should reject unavailable required tools")
    async with SQLiteExecutionStore(path):
        pass
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "mcp-session"]


async def test_wrong_provider_result_becomes_structured_error() -> None:
    class Broken:
        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            return None

    run = await AgentHarness(Broken(), FakeToolExecutor()).run(Session("s"), "hi")
    assert run.status == "failed"
    assert run.error is not None and run.error.code == "MALFORMED_MODEL_RESPONSE"
