# MCP tools and client lifecycle

The runtime has an MCP-backed `ToolExecutor` and a separate mock factory
server with three read-only tools:

- `get_machine_status`
- `list_open_work_orders`
- `get_safety_procedure`

Start the mock server over stdio with:

~~~powershell
uv run minder-mock-mcp
~~~

The process waits silently for an MCP client on stdin; that is expected. A
client normally starts it as a subprocess:

~~~python
import anyio
from mcp import StdioServerParameters

from minder_harness.core import ToolCall
from minder_harness.mcp import MCPToolExecutor, SDKMCPClient


async def main():
    server = StdioServerParameters(command="uv", args=["run", "minder-mock-mcp"])
    async with SDKMCPClient(server, startup_timeout_seconds=5) as client:
        executor = MCPToolExecutor(client, approved_tools=["get_machine_status"])
        tools_for_model = await executor.discover_tools()
        print([tool.name for tool in tools_for_model])
        result = await executor.execute(
            ToolCall("demo_1", "get_machine_status", {"machine_id": "CNC-04"})
        )
        print(result.to_dict())


anyio.run(main)  # CLI entry point only, not once per request.
~~~

The application opens one client per server/configuration/security context on one
asyncio event loop. Startup connects and discovers under its own timeout, before
runs start. Keep the context open across runs; provider, executor, loop and harness
I/O are async (use `await harness.run(...)`). There is no separate sync runtime.

The same SDK session retains its input/output schema cache: one discovery pass per
connection (possibly multiple pages), not per tool call. Executor catalogs are
versioned; `await executor.discover_tools(refresh=True)` explicitly reloads them.
Tool results remain fresh. On connection failure, a single lifecycle owner
reconnects on the next discovery and reloads schemas. Each pending call checks its
validated catalog version before dispatch; retries revalidate arguments and safety.
Reconnect never replays a tool itself or resets the run deadline.

The client limits in-flight requests to eight by default (`max_in_flight`).
Capacity, lock, reconnect and retry waits within a run consume remaining time and
respond to cancellation. Startup reuse saves overhead; it cannot make an
unrealistically short run timeout succeed.

Discovery never grants authority by itself: only tools both advertised by the
server and present in `approved_tools` are exposed. Arguments are validated
locally before dispatch. The adapter owns one optional retry for known
transient failures, and only for tools declared safe to repeat.

Cancel stops local waiting for that request through the SDK without closing the
shared session or cancelling other runs. A closed connection interrupts its pending
requests. At shutdown the application must stop admission and finish/cancel active
runs before exiting the client context; `aclose()` aborts remaining I/O and waits
for SDK process cleanup. This bounded cleanup grace can exceed the request timeout.
Permission/configuration errors are not retried.
`error.details.outcome_unknown` separately records whether dispatch may have
happened. Unsafe identical calls with an uncertain outcome are blocked by that
executor, including after cancellation. Without a durable recorder, this guard is in memory, matches exact
tool name/arguments, and needs outcome reconciliation before it can be released.

The tested transports are local stdio and the SDK's in-process server. The SDK's
input-required replay loop is disabled so the executor owns the attempt budget.
HTTP Retry-After and other HTTP-specific retry behavior are deferred.

The test suite includes real subprocess servers that delay, drop the connection,
ignore cancellation, or ask for more input. A parent watchdog bounds each stdio
test; tests check actual connection/discovery/invocation counts and process exit.
Async lifecycle tests cover cancellation isolation, capacity waits, reconnect
coalescing, schema changes, provider cancellation, and shutdown. HTTP transport
and production load have not been validated.


See [persistence and run lifecycle](persistence.md) for durable attempt recording
and [testing](../../tests/README.md) for fault scenarios.
