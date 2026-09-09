"""Real stdio failure fixture. Only writes its invocation log under pytest's tmp_path."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from time import sleep

import anyio
from mcp.server import MCPServer
from mcp.types import InputRequiredResult, Tool, ToolAnnotations


def main() -> None:
    mode, log_name = sys.argv[1:]
    log = Path(log_name)

    def record(event: str) -> None:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    record("server_started")
    if mode == "discovery_timeout":
        sleep(30)

    class CountingServer(MCPServer):
        async def list_tools(self) -> list[Tool]:
            record("tools_list")
            return await super().list_tools()

    server = CountingServer(name="failure-fixture")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def probe() -> dict[str, bool] | InputRequiredResult:
        previous_calls = sum(
            json.loads(line)["event"] == "tool_called" for line in log.read_text().splitlines()
        )
        record("tool_called")
        if mode == "drop" or (mode == "retry_success" and previous_calls == 0):
            os._exit(17)  # Simulate the remote process dying after dispatch.
        if mode == "stubborn":
            sleep(30)  # Intentionally ignore protocol cancellation; SDK must stop this process.
        if mode in {"slow", "cancel"}:
            await anyio.sleep(30)
        if mode == "business_error":
            raise ValueError("Unknown machine")
        if mode == "input_required":
            return InputRequiredResult(input_requests={}, request_state="pending")
        return {"ok": True}

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
