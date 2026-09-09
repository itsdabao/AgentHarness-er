"""Offline demo server: FAKE model, actual RPC + separate stdio MCP + SQLite."""

from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import uvicorn
from mcp import StdioServerParameters

from minder_harness.core import AgentHarness, Message, ModelResponse, ToolCall, ToolDefinition
from minder_harness.core.ports import ModelExecutionContext
from minder_harness.mcp import MCPToolExecutor, SDKMCPClient
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.rpc import create_app
from minder_harness.service import AgentService


class OfflineDemoProvider:
    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        if messages[-1].role == "tool":
            return ModelResponse(content={"fake_model": True, "tool_result": messages[-1].content})
        task = str(messages[-1].content)
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "demo",
                    "get_machine_status",
                    {
                        "machine_id": "UNKNOWN" if "UNKNOWN" in task else "CNC-04",
                        "delay_ms": 5000 if "delay_ms=5000" in task else 0,
                    },
                ),
            )
        )


@asynccontextmanager
async def runtime(database: str) -> AsyncIterator[AgentService]:
    server = StdioServerParameters(command=sys.executable, args=["-m", "minder_mock_mcp.server"])
    async with SQLiteExecutionStore(database) as store:
        async with SDKMCPClient(server, startup_timeout_seconds=10) as mcp:
            executor = MCPToolExecutor(mcp, ["get_machine_status"])
            tools = await executor.discover_tools()
            async with AgentService(
                AgentHarness(OfflineDemoProvider(), executor), store, tools=tools
            ) as service:
                yield service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default="var/offline-demo.db")
    args = parser.parse_args()
    uvicorn.run(
        create_app(lambda: runtime(args.database)),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
