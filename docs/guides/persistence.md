# Persistence and run lifecycle

The runtime uses SQLite (aiosqlite), a transport-neutral AgentService, and a small
deterministic context builder. The same AgentHarness/AgentLoop is used for both
in-memory tests and durable runs; there is no second agent loop.

Run the offline demo (actual MCP protocol, explicitly fake model):

~~~powershell
uv run python examples/phase3_demo.py
uv run python examples/phase3_demo.py --session <session_id_printed_above>
uv run python examples/phase3_demo.py --cancel-after 0.1
~~~

The demo defaults to var/harness.db and prints JSON session/run identities, a run
snapshot, and a separate event page. Use --database to select another local file.
Restarting the command preserves sessions and traces. Resume starts a NEW run;
it never automatically replays an interrupted tool. The demo is not a real-LLM test.

Application usage: open SQLiteExecutionStore and MCP clients first, then enter
AgentService. Submit with await service.submit_task(session_id, content), inspect
with get_run/list_run_events, cancel with cancel_run, or await wait_run for a
terminal snapshot. Exit the service before closing MCP clients or the store.
The service stops admission and cancels/awaits owned tasks on shutdown.

Critical writes are awaited. State transitions and their events share a transaction;
tool attempts have a durable intent before dispatch and a recorded outcome after.
A storage failure quarantines the store and stops new work; it is not a retryable
tool error. SQLite cannot atomically commit an external side effect with its result:
unresolved unsafe attempts remain blocked after restart. Explicit operator
resolve_attempt with an evidence note is available on the store, never as an agent
tool. Configure a stable server_scope per MCP server/security context, without secrets.

One runtime owns a database through an OS-held .owner lock; crashes release the
lock without deleting the file. The store uses WAL, full synchronization, and
ordered schema migrations. Do not run multiple supervisors against one open store,
or put this demo database on an unsupported shared/network filesystem.

Full audit history is retained separately from a compact versioned task_state.
Explicit user constraints, source-linked historical observations and open questions
are data, not tool authority or current readings. The context builder preserves
required instructions/current task and complete tool exchanges; optional old history
is bounded. Defaults: 6000 JSON characters for state, 24000 for selected context
including tool definitions. These are NOT token counts. Required overflow produces
CONTEXT_BUDGET_EXCEEDED before a provider call; no automatic summarizing LLM is used.
Partial transcripts get explicit outcome markers in a continuation view, while
original audit data remains unchanged. Uncommitted final answers are excluded.

Known limits: one process, one event loop, cooperative async adapters, no remote
rollback or exactly-once guarantee. Very large histories still load from SQLite
before selection; retention/large-history optimization is deferred.

HTTP RPC and provider configuration are covered in the [RPC guide](rpc.md).
For recorded validation and its limits, see the [test report](../reports/phase-5-test-report.md)
and [interface validation notes](interfaces.md#kiểm-chứng).
