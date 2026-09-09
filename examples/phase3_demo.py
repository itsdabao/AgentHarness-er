"""Offline Phase 3 demo: real MCP protocol + SQLite, deterministic fake model."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

import anyio

from minder_harness.core import AgentHarness, Message, ModelResponse, ToolCall, ToolDefinition
from minder_harness.core.ports import ModelExecutionContext
from minder_harness.mcp import MCPToolExecutor, SDKMCPClient
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.service import AgentService
from minder_mock_mcp.server import build_server


class DemoProvider:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        if self.delay:
            await anyio.sleep(self.delay)
        if messages[-1].role == "tool":
            return ModelResponse(
                content={"summary": "Tool result received", "result": messages[-1].content}
            )
        return ModelResponse(
            tool_calls=(ToolCall("demo-status", "get_machine_status", {"machine_id": "CNC-04"}),)
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="var/harness.db")
    parser.add_argument("--session", help="Continue a session printed by a previous invocation")
    parser.add_argument(
        "--cancel-after", type=float, help="Use a slow fake provider and cancel the run"
    )
    args = parser.parse_args()
    async with SQLiteExecutionStore(args.database) as store, SDKMCPClient(build_server()) as client:
        executor = MCPToolExecutor(
            client,
            ["get_machine_status"],
            server_scope="demo-factory/read-only",
        )
        tools = await executor.discover_tools()
        provider = DemoProvider(10 if args.cancel_after is not None else 0)
        async with AgentService(AgentHarness(provider, executor), store, tools=tools) as service:
            session_id = args.session or (await service.create_session()).session_id
            run = await service.submit_task(
                session_id,
                "Check CNC-04",
                constraints=["Read-only factory tools"],
            )
            print(
                json.dumps({"session_id": session_id, "run_id": run.run_id, "status": run.status})
            )
            if args.cancel_after is not None:
                await anyio.sleep(max(0, args.cancel_after))
                await service.cancel_run(run.run_id)
            result = await service.wait_run(run.run_id)
            print(json.dumps(result.to_dict(), ensure_ascii=False))
            print(json.dumps(await service.list_run_events(run.run_id), ensure_ascii=False))


if __name__ == "__main__":
    anyio.run(main)
