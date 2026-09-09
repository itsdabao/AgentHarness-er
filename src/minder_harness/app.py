"""Composition root: one process, one event loop, one database owner."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from mcp import StdioServerParameters

from .core.harness import AgentHarness
from .mcp import MCPToolExecutor, SDKMCPClient
from .persistence import SQLiteExecutionStore
from .providers.gemini import GeminiProvider
from .rpc import create_app
from .service import AgentService

APPROVED_TOOLS = ("get_machine_status", "list_open_work_orders", "get_safety_procedure")


@dataclass(frozen=True)
class Settings:
    database: str = "var/harness.db"
    model: str = "gemini-2.5-flash"
    max_attempts: int = 2
    request_timeout: float = 30
    input_budget: int = 32768
    output_tokens: int = 2048

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database=os.getenv("MINDER_DATABASE", "var/harness.db"),
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            max_attempts=int(os.getenv("MINDER_PROVIDER_MAX_ATTEMPTS", "2")),
            request_timeout=float(os.getenv("MINDER_PROVIDER_TIMEOUT", "30")),
            input_budget=int(os.getenv("MINDER_INPUT_BUDGET", "32768")),
            output_tokens=int(os.getenv("MINDER_OUTPUT_TOKENS", "2048")),
        )


@asynccontextmanager
async def runtime(settings: Settings, key: str) -> AsyncIterator[AgentService]:
    if not key:
        raise ValueError("Set GEMINI_API_KEY in the environment before starting.")
    server = StdioServerParameters(command=sys.executable, args=["-m", "minder_mock_mcp.server"])
    # Close the service first so no run touches dependencies being shut down.
    async with httpx.AsyncClient(follow_redirects=False) as http:
        provider = GeminiProvider(
            http,
            key,
            model=settings.model,
            max_attempts=settings.max_attempts,
            request_timeout=settings.request_timeout,
            input_budget=settings.input_budget,
            output_tokens=settings.output_tokens,
        )
        async with SQLiteExecutionStore(Path(settings.database)) as store:
            async with SDKMCPClient(server, startup_timeout_seconds=10) as client:
                executor = MCPToolExecutor(
                    client,
                    APPROVED_TOOLS,
                    server_scope="mock-factory/read-only",
                    call_timeout_seconds=3,
                )
                tools = await executor.discover_tools()
                if {t.name for t in tools} != set(APPROVED_TOOLS):
                    raise ValueError("Required MCP tools are unavailable.")
                async with AgentService(
                    AgentHarness(provider, executor), store, tools=tools
                ) as service:
                    yield service


def main() -> None:
    parser = argparse.ArgumentParser(description="Local single-worker Agent Harness RPC server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings.from_env()
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        parser.error("Set GEMINI_API_KEY. Credentials are not read from source or CLI arguments.")
    app = create_app(lambda: runtime(settings, key), secrets=(key,))
    # No reload/multiple workers: SQLite ownership enforces one runtime process.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
