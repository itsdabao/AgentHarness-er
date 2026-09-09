"""Actual adapter subprocess; the parent test enforces a hard watchdog."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import monotonic

import anyio
from mcp import StdioServerParameters

from minder_harness.core import CancellationToken, ToolCall, ToolExecutionContext
from minder_harness.core.models import JSONValue
from minder_harness.mcp import MCPClientError, MCPToolExecutor, SDKMCPClient


async def main() -> None:
    mode, log_name = sys.argv[1:]
    log = Path(log_name)
    client = SDKMCPClient(
        StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("mcp_failure_server.py")), mode, log_name],
        ),
        startup_timeout_seconds=2.0 if mode == "discovery_timeout" else 5.0,
    )
    executor = MCPToolExecutor(
        client,
        ["probe"],
        # Reconnect tests include process cleanup + cold Python/SDK startup.
        # Slow/stubborn request-timeout tests still use the independent 2s limit.
        call_timeout_seconds=5.0 if mode in {"retry_success", "drop"} else 2.0,
        max_attempts=2 if mode in {"retry_success", "drop", "input_required"} else 1,
        retry_delay_seconds=0,
    )
    token = CancellationToken()
    events: list[dict[str, JSONValue]] = []

    async def emit(kind: str, payload: dict[str, JSONValue] | None) -> None:
        events.append({"type": kind, "payload": payload})

    async def cancel_after_dispatch() -> None:
        while True:
            if '"tool_called"' in log.read_text():
                token.cancel()
                return
            await anyio.sleep(0.01)

    started = monotonic()
    try:
        async with client:
            started = monotonic()  # Request + cleanup bound, separate from successful startup.
            await executor.discover_tools()
            async with anyio.create_task_group() as tasks:
                if mode == "cancel":
                    tasks.start_soon(cancel_after_dispatch)
                result = await executor.execute(
                    ToolCall("probe_call", "probe", {}),
                    ToolExecutionContext(token, 15.0, emit),
                )
                if mode == "reuse":
                    for number in range(2):
                        result = await executor.execute(ToolCall(f"reuse_{number}", "probe", {}))
                        assert result.status == "succeeded"
                tasks.cancel_scope.cancel()
    except MCPClientError as error:
        if mode != "discovery_timeout":
            raise
        print(json.dumps({"code": error.code, "elapsed": monotonic() - started}))
        return
    assert mode != "discovery_timeout", "Startup should time out"
    print(
        json.dumps(
            {
                "result": result.to_dict(),
                "elapsed": monotonic() - started,
                "events": events,
            }
        )
    )


if __name__ == "__main__":
    anyio.run(main)
