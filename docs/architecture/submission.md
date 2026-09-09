# Design decisions and limitations

The runtime is a Python application with a custom asynchronous model-tool-result
loop. A platform submits tasks through a thin JSON-RPC HTTP adapter. The adapter
validates requests, calls AgentService and maps results; it owns no reasoning,
database or tool execution logic. A different transport can use the same service.

AgentService owns run tasks, cancellation tokens and admission. AgentHarness
coordinates one run and AgentLoop performs bounded model/tool steps. The core
depends on small provider and tool protocols. GeminiProvider translates messages,
function calls and errors using the generateContent REST API through HTTPX.
MCPToolExecutor independently validates and executes approved tools through the
official MCP client. The three read-only factory tools run in a separate mock
server; neither factory rules nor SDK classes enter the loop.

ExecutionStore is implemented by SQLite/aiosqlite. State transitions and events
commit together in short serialized transactions. Each tool attempt records intent
before dispatch and outcome afterward. No transaction spans external I/O. A crash
between remote execution and result recording is inherently ambiguous; an unsafe
matching call remains blocked across restart until explicit reconciliation.
Recovery marks unfinished runs interrupted instead of replaying tools.

Cancellation is persisted and propagated to asynchronous I/O and retry waits.
Checks after external work prevent late responses from completing cancelled runs
or scheduling further steps. Cancelling local waiting does not roll back remote
effects. Failed critical recording quarantines the runtime rather than allowing
untraceable work. Startup checks the MCP catalog; shutdown stops admission and
drains/cancels runs before closing dependencies.

Retries belong to adapters. Known transient failures may retry once within the
same deadline; tool repeats additionally require a safety decision. Gemini 429
needs a usable provider delay hint; credentials, malformed output and unknown
errors fail without retry. Attempt events explain each decision before waiting.
Logical steps are distinct from network attempts. There is one real provider and
no automatic fallback.

Audit history and compact task context are separate. Deterministic selection
preserves required input and complete tool exchanges. The provider applies a
conservative serialized-size estimate with output headroom; observed token counts
are reported separately, not represented as complete billing data. Opaque
continuation signatures stay internal, and private thought text is discarded.
Callers poll ordered persisted events by cursor, using fixed progress templates.

The runtime targets one local process and one database owner. Its first scaling
limits are whole-session history loading, one serialized SQLite connection and
unbounded growth of audit data. Production work would add retention and bounded
history queries, admission/backpressure, authentication/authorization and worker
coordination. Actual tool-side idempotency/reconciliation would be needed for
stronger side-effect guarantees. The implementation does not claim exactly-once
execution or secure OS sandboxing.

The CLI and local Web UI use the same RPC API; neither owns an agent loop or opens
the database. Simple and Detailed views present the same server-confirmed state.

Offline tests cover wire translation, actual HTTP/MCP subprocesses, faults,
cancellation, restart and crash windows, using shared runtime and fault helpers.
See the [test report](../reports/phase-5-test-report.md) for dated results and
limits. Prior backend Gemini smoke runs are noted in the
[interface guide](../guides/interfaces.md#kiểm-chứng); these do not establish
current model availability, client visual correctness or context quality.
