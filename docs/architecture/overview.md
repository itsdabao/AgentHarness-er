# Minder Agent Harness architecture

Minder is a custom asynchronous agent runtime with a thin JSON-RPC API, persistent
execution state and approved MCP tools. A model proposes actions; the runtime owns
execution limits, recording, cancellation and tool authority. The factory scenario
is a separate mock integration, not a dependency of the agent algorithm.

This document describes the implemented system and its limits. The shorter
[design note](submission.md) summarizes the main tradeoffs; operational commands
remain in the [RPC](../guides/rpc.md) and [interface](../guides/interfaces.md) guides.

## 1. Components and dependency boundaries

~~~mermaid
flowchart TB
    Clients["Platform / CLI / Web UI"] --> RPC["HTTP JSON-RPC adapter"]
    subgraph Runtime["One Python process and asyncio event loop"]
        RPC --> Service["AgentService: admission and task ownership"]
        Service --> Harness["AgentHarness: one-run coordination"]
        Harness --> Loop["AgentLoop: model / tool / result"]
        Loop --> Provider["LLMProvider: Gemini adapter"]
        Loop --> Executor["ToolExecutor: MCP adapter"]
        Service --> Store["ExecutionStore: SQLite"]
        Loop -. "awaited events through service callback" .-> Store
        Executor -. "AttemptRecorder: intent and outcome" .-> Store
    end
    Provider <-->|"model request / response"| Gemini["Gemini API"]
    Executor <-->|"SDKMCPClient over stdio"| MCP["Separate mock MCP process"]
    MCP --> Tools["Three read-only factory tools"]
~~~

Solid arrows show calls or communication, not an import graph. Dashed arrows show
recording through interfaces: the loop does not import SQLite or issue SQL.
Operational data access is restricted to MCP tools; model requests use the
separate provider connection.

| Component | Owns | Does not own | Source |
| --- | --- | --- | --- |
| Composition root | Configuration and dependency startup/teardown | Agent decisions | [app.py](../../src/minder_harness/app.py) |
| RPC adapter | Request validation, protocol errors and public response mapping | Model loop or tool execution | [rpc.py](../../src/minder_harness/rpc.py) |
| AgentService | Durable submission, active tasks/tokens, admission, recovery and shutdown | Provider wire formats | [service.py](../../src/minder_harness/service.py) |
| AgentHarness / AgentLoop | Run coordination; bounded model/tool/result iterations | HTTP, SQL, MCP SDK or factory rules | [harness.py](../../src/minder_harness/core/harness.py), [loop.py](../../src/minder_harness/core/loop.py) |
| Provider adapter | Model wire format, usage, provider error/retry policy | Executing model-requested tools | [gemini.py](../../src/minder_harness/providers/gemini.py), [gemini_codec.py](../../src/minder_harness/providers/gemini_codec.py) |
| MCP executor / client | Allowlist, validation, attempt policy; SDK transport and connection lifecycle | Agent conversation or business planning | [executor.py](../../src/minder_harness/mcp/executor.py), [client.py](../../src/minder_harness/mcp/client.py) |
| ExecutionStore | Transactions, snapshots, audit records, recovery and replay guard | External effects | [sqlite.py](../../src/minder_harness/persistence/sqlite.py) |
| Mock factory server | Tool schemas and fixture data | Agent sessions or orchestration | [server.py](../../src/minder_mock_mcp/server.py) |

The core uses plain JSON-compatible dataclasses and small async protocols:
`LLMProvider.generate`, `ToolExecutor.execute`, `EventEmitter` and
`AttemptRecorder`. The application uses `ExecutionStore`, which also implements
the attempt-recording contract. See [ports](../../src/minder_harness/core/ports.py)
and [store protocol](../../src/minder_harness/core/store.py).

A different transport can call AgentService without changing the loop. A different
provider translates the same messages and tool definitions. The implementation
uses SDKs for infrastructure, but no agent framework supplies the execution loop.

## 2. Execution flow and ownership

### Task execution pipeline

The component diagram above shows ownership. This pipeline shows how a task
progresses, including the tool-result feedback that drives the next model step.

~~~mermaid
flowchart TD
    Submit["CLI / Web UI / platform: submit task"] --> RPCInput["HTTP RPC: validate request"]
    RPCInput --> Admit["AgentService: admit run and commit queued state"]
    Admit --> Background["Start background run; return snapshot to caller"]
    Background --> Check{"Cancellation, deadline and budgets permit work?"}
    Check -->|Yes| Context["Build bounded context"]
    Context --> Model["Provider adapter calls LLM"]
    Model --> Decision{"Validated model response"}
    Decision -->|Tool calls| Validate["MCP executor: allowlist, schema and replay checks"]
    Validate -->|Approved| Intent["Commit attempt intent"]
    Intent --> Dispatch["Separate MCP server executes tool"]
    Dispatch --> Outcome["Record attempt outcome"]
    Outcome --> Feedback["Feed correlated tool result or error into the loop"]
    Validate -->|Rejected request| Feedback
    Feedback --> Check
    Decision -->|Final answer| FinalCheck["Recheck cancellation and deadline"]
    FinalCheck --> Terminal["Commit terminal state and event"]
    Check -->|No| Terminal
    Model -->|Provider failure after allowed attempts| Terminal
    Decision -->|Invalid or empty response| Terminal
    Terminal --> Observe["Client reads snapshot and ordered event history"]
~~~

The LLM proposes calls; it never invokes MCP directly. Multiple tool calls execute
serially. Adapter retries are bounded attempts inside the provider or executor,
not a restart of this pipeline. Tool errors can become model feedback; storage
failures instead stop execution through the fail-closed path described below.
If storage is unavailable, a terminal commit is not guaranteed.

Clients can poll during execution, not only after completion. Cancellation is a
separate control request observed at execution boundaries and by cooperative
adapters; it does not undo remote effects. Intermediate event writes are omitted
for readability: persistence is part of execution, not final-only logging.

### Startup and run lifecycle

At startup, the composition root creates the shared provider HTTP client, opens
SQLite with database ownership and migrations, then opens the MCP session. It
verifies the approved tool catalog before entering AgentService and admitting work.
Service startup recovers unfinished durable runs. Readiness confirms this local
startup path, not that a Gemini key/model/quota will satisfy the next request.

For an accepted task:

1. RPC validates input and delegates to `submit_task`. In one transaction the
   service/store create a queued run, user message, task-state revision and event.
   The service owns the background task; submission returns a run snapshot without
   waiting for model completion.
2. The harness starts the run. Each loop iteration checks cancellation, remaining
   time and step budget, then builds the bounded model context.
3. The provider returns either a final answer or tool calls. Invalid/empty
   responses fail structurally. Provider-specific wire data stays at the adapter.
4. Each tool decision receives an execution identity. The executor checks approval,
   catalog/schema, time budget and replay safety, records intent, then calls MCP.
5. The executor records the attempt outcome. The loop emits the result and feeds a
   correlated tool message into the next model request. Multiple calls from one
   response are executed serially; parallel tool execution is not implemented.
6. A final answer terminates the loop only if cancellation/deadline checks still
   permit it. The store commits the authoritative terminal state and event.

A tool failure is normally feedback to the model, which can change its approach
or explain missing data. An exhausted provider request fails the run. A storage
failure follows a separate fail-closed path rather than becoming retryable tool
feedback.

`completed` means the runtime accepted a final answer, not that its factual or
business correctness has been proven. The local-model evaluation demonstrates
why those two judgments must remain separate.

Default run limits are eight model steps, eight logical tool calls and 120 seconds.
RPC caps caller-supplied limits at 100 steps, 100 calls and 300 seconds.
Adapter attempts do not count as new logical model steps/tool calls. The loop
reserves a model step for receiving tool results: a tool request on the final
allowed step terminates as limit_exceeded instead of dispatching more work.

One active run per session is enforced both by application coordination and a
partial unique SQLite index over active statuses. Different sessions can run
concurrently; there is no global admission queue or fairness scheduler.

## 3. Durable state and traceability

The [schema](../../src/minder_harness/persistence/schema.py) separates:

- **Sessions and messages:** conversation identity, ordered exchanges and whether
  a message was accepted into execution history.
- **Runs:** status, limits, timestamps, output/error and usage snapshots.
- **Events:** ordered per-run audit records, addressed by sequence cursor.
- **Attempts:** execution ID, attempt number, server scope, canonical call key,
  intent/outcome and uncertainty.
- **Task state:** bounded objective, constraints, observations and open questions
  with source references; not the authoritative run state.

Provider tool-call IDs correlate exchanges. Runtime execution IDs distinguish
logical executions, and attempt numbers distinguish infrastructure retries.
The database enforces uniqueness for event sequence and execution/attempt pairs.

SQLite uses WAL, foreign keys, FULL synchronization and one serialized connection.
Short `BEGIN IMMEDIATE` transactions commit state transitions and their events
together. Critical recording is awaited; transactions are cancellation-shielded
while being completed or rolled back. No transaction spans model or MCP I/O.

This is explicit snapshot/projection persistence plus an audit log, not a general
event-sourcing system rebuilt entirely from events. Recording failures quarantine
the store and stop further work. Domain failures such as a conflicting active run
roll back without automatically poisoning the store.

### External calls are not database transactions

The intended sequence is:

~~~text
commit attempt intent -> dispatch MCP call -> receive result -> commit outcome
~~~

| Failure window | Durable interpretation | Recovery behavior |
| --- | --- | --- |
| Intent cannot be committed | No authorized recorded attempt to dispatch | Do not call the tool; stop on storage failure |
| Crash after intent, before a recorded outcome | Dispatch may or may not have happened | Keep the ambiguity; do not automatically replay the interrupted run |
| Remote success, outcome write/acknowledgment lost | Local process cannot safely infer the remote outcome | Quarantine on storage failure; reconcile from durable evidence before unsafe repetition |
| Outcome committed, final answer not committed | Tool evidence exists, but no accepted final answer | Retain trace; do not expose an uncommitted answer as completed |

For unsafe calls, an unresolved matching attempt blocks replay across new executors,
sessions and restarts. Matching uses stable server/security scope plus tool name
and canonical arguments, not semantic equivalence of business operations. The
store exposes operator-only reconciliation with an evidence note; it is not an
agent tool or a public RPC method.

Without a durable recorder, the executor's guard is only in memory. Neither mode
provides exactly-once remote execution. The demo tools are read-only; unsafe
side-effect scenarios are exercised with controlled test fixtures.

An OS-held ownership lock prevents a second runtime from recovering the same live
database. Recovery marks queued/running runs interrupted and pending cancellations
cancelled, without replaying tools or restarting deadlines. Resuming a session
means explicitly submitting a new run with retained history, not restoring a
suspended Python task or MCP connection.

## 4. MCP lifecycle and tool authority

The application keeps one SDKMCPClient per server/configuration/security context
on the same event loop as the runtime. A lifecycle owner enters and exits the SDK
context; agent sessions are persisted conversations, while MCP sessions are live
resources that must be re-established after restart.

Discovery caches tool definitions and SDK schemas per connection, not tool
results. Explicit refresh or reconnect reloads the versioned catalog. Dispatch
checks the catalog version used for validation; retries revalidate tool existence,
arguments and retry safety rather than blindly reusing old metadata.

The executor exposes only the intersection of discovered tools and the configured
allowlist. New server capabilities do not grant new authority. Unknown/unapproved
tools and invalid arguments are rejected before tool dispatch and remain visible
as logical failures in the event trace.

Connection recovery is single-flight. Capacity is bounded to eight in-flight
client requests by default; waiting for capacity, reconnect or retry within a run
consumes its remaining deadline and observes cancellation. A short-lived waiter
does not cancel another run's shared connection startup. Reconnect does not replay
a tool or reset run time. The SDK input-required replay loop is disabled so the
executor owns its attempt budget.

Retry safety currently uses annotations from the approved MCP server: explicitly
read-only, or explicitly idempotent and not marked destructive. These are server
claims, not proof that an arbitrary third-party tool is harmless. Approving and
configuring the server is part of the trust boundary.

## 5. Errors, retries and cancellation

### Retries belong to adapters

The loop does not implement a generic automatic retry engine. After a tool error,
a model can request another logical call, which consumes another tool-call budget.
That is different from an adapter retry of the same execution.

| Condition | Current policy |
| --- | --- |
| MCP tool returns a logical error | Record failure, return tool feedback; no automatic transport retry |
| Unknown/unapproved tool or invalid arguments | Reject locally; no automatic retry |
| Known transient MCP transport failure | Retry only if retry-safe, attempt budget and deadline permit |
| Uncertain unsafe tool outcome | Keep uncertainty separate from retryability and block matching replay |
| Gemini timeout, connection failure or 5xx | At most one retry within the same run deadline |
| Gemini 429 | Retry only with a usable Retry-After/RetryInfo delay that fits the remaining time |
| Invalid credentials/model/request, malformed provider output | Fail without retry or provider fallback |
| Critical storage failure | Stop/quarantine; do not classify as a tool/provider retry |

The default MCP budget is two attempts including the first, with a configurable
fixed delay. Gemini supports one or two attempts. This intentionally small policy
does not add exponential-backoff machinery or a second SDK retry layer.
Retry plans/reasons are recorded before waiting. Per-attempt timeouts are clipped
to the remaining run time; a retry never creates a fresh run budget.

### Cancellation has a commit boundary

AgentService persists the cancellation request and signals the run's token.
The loop checks before scheduling more work and after external awaits. Async
provider/MCP waits and retry delays are cancellable; request cancellation does
not tear down the shared MCP session or cancel unrelated sessions.

A queued run can become cancelled without starting a provider request. A running
run may first report cancel_requested. If cancellation is committed before the
terminal transition, the store does not accept a late answer as successful output.
If completion committed first, a later cancel is a no-op and completed remains
authoritative. Late data can remain diagnostic audit data without entering the
accepted continuation.

Clients must wait for a persisted terminal snapshot, not treat a button click or
cancel acknowledgment as proof the tool has stopped. Disconnecting a client or
stopping event polling does not cancel a server-owned task.

Shutdown stops admission, signals/drains owned runs, then closes MCP, SQLite and
provider HTTP resources in reverse ownership order. Cleanup shields/grace periods
can outlast the request deadline. Cooperative cancellation cannot forcibly stop
arbitrary blocking Python or roll back a remote effect.

## 6. Context and provider boundaries

Audit history, execution state and model context have different purposes.
TaskState keeps source-linked objectives, explicit constraints, observations and
open questions; these values never grant tool permissions or determine run status.

The deterministic context builder preserves required input and complete selected
tool exchanges under a default 24,000 JSON-character budget. Task state is bounded
to 6,000 JSON characters. Required input that cannot fit fails before the provider
call; optional historical material can be dropped. These are character budgets,
not token counts, and do not imply immunity to lost-in-the-middle behavior.

On continuation, missing/uncertain outcomes get explicit markers in a model-facing
view. Original audit messages are not rewritten and uncommitted final answers are
not promoted into accepted history. There is no extra summarizing model, vector
store or state-machine framework. See
[context.py](../../src/minder_harness/core/context.py).

GeminiProvider uses HTTPX directly. Its codec maps messages, function calls,
results and errors to the provider-neutral contract. Opaque continuation metadata
is retained internally when required for faithful follow-up requests, while
private thought text is discarded. Provider continuation is excluded from the
public API; changing providers/models is not a transparent configuration swap
for an existing continuation.

Before HTTP dispatch, the provider applies a conservative serialized UTF-8-size
estimate plus overhead and reserved output headroom. This is not an exact
tokenizer or billing guarantee. Reported input/output token usage is accumulated
from accepted successful model steps; failed or ambiguous request charges and
missing usage are not fully known.

Local GGUF evaluation uses an adapter under tests/ with the same core and MCP
fixtures. It is not wired into the application and has no automatic Gemini
fallback. It demonstrates reuse of the boundary, not production equivalence
between providers.

## 7. RPC, observation and security

The HTTP API is a JSON-RPC 2.0 subset with named parameters and a single request
per body; batches and positional parameters are unsupported. Methods cover
create/get/list sessions, submit_task, get_run, list_run_events and cancel_run.
Body limits and input validation live at the transport edge.

Successful protocol handling returns an application envelope with ok/data/error.
A domain failure appears inside that result; JSON-RPC protocol errors instead use
the outer error member. A failed run can therefore be returned by a successful
get_run operation. The full contract and examples are in the [RPC guide](../guides/rpc.md).

Run snapshots and event history are separate responses. Clients poll committed
events by per-run sequence cursor, drain pages, and drain remaining events after
observing a terminal snapshot. The CLI and Web UI share this API; neither opens
SQLite or executes tools. Session listing supports paging and reattaching to the
latest run, not automatic replay.

Progress messages use fixed templates over real event types and error codes.
The Web pipeline highlights the selected/latest event's owning component; it is
an explanatory view of recorded execution, not a profiler or visualization of
private model reasoning. There is no token-level streaming or SSE/WebSocket
transport in the current implementation.

Submission is not idempotent: if a submit response is lost, the client must not
automatically submit again. Session listing can help locate a latest run, but
there is no general run-search API or idempotency-key contract to resolve every
ambiguous submission.

The server binds to loopback, with no authentication or multi-user authorization.
Public projections omit internal continuation fields and redact configured
credentials. The Web UI renders tool output as text and restricts cross-origin
RPC requests; Simple/Detailed modes are presentation, not permission levels.
The local database contains detailed audit data and is not an encrypted secret
store. Review traces before sharing them.

The allowlist and subprocess boundary are not an OS sandbox. The mock server is
trusted application code with read-only demo data; integrating real factory
systems would require independent authorization, safety and side-effect controls.

## 8. Verification and remaining production work

| Property | Evidence in the repository |
| --- | --- |
| Multi-step feedback, limits and malformed responses | [Core tests](../../tests/test_core.py), [context tests](../../tests/test_context.py) |
| MCP faults, schema changes, reconnect and cancellation isolation | [Client tests](../../tests/test_mcp_client.py), [lifecycle tests](../../tests/test_mcp_lifecycle.py) |
| Atomic state/event writes, crash windows, uncertain outcomes and recovery | [Persistence tests](../../tests/test_persistence.py), [database lifecycle tests](../../tests/test_database_lifecycle.py) |
| Provider wire handling and HTTP RPC behavior | [Gemini tests](../../tests/test_gemini.py), [RPC socket test](../../tests/test_rpc_socket.py) |
| Dependent tool chains with controlled model decisions | [Multi-step fixtures/tests](../../tests/test_multistep_scenarios.py) |
| Real local-model decisions through the same core | [Local-model report and raw traces](../reports/local-model-test-report.md) |

The recorded regression run on 9 September 2026 passed 184 offline tests. A
separate 22-run local-model evaluation includes genuine model failures as well as
successful guided chains and cancellation. These counts measure different things.
The [earlier test report](../reports/phase-5-test-report.md) remains a dated record
of 172 tests, not a contradictory current total.

Gemini codec tests use mocked provider responses. Historical backend smoke runs
are noted in the [interface guide](../guides/interfaces.md#kiểm-chứng), but they do
not establish current credentials/quota, visual acceptance of the clients or
model-quality parity with the local evaluation. Tested MCP transports are local
stdio and SDK in-process transport; MCP-over-HTTP and production load remain
unvalidated. HTTP RPC tests do not establish MCP-over-HTTP support.

The main production limits and next steps are:

1. **Load and storage growth:** whole-session history is loaded before context
   selection; SQLite operations share one serialized connection and audit retention
   is unbounded. Add bounded history queries, retention and measured admission
   backpressure before pursuing more workers.
2. **External side effects:** canonical-call replay guards cannot replace
   business-level idempotency keys and reconciliation at the tool service.
3. **Deployment and isolation:** add authentication, authorization, secret handling
   and stronger tool isolation before exposing the API outside the local machine.
4. **Distributed ownership:** replacing SQLite alone is insufficient; multiple
   workers need leases/fencing, coordinated recovery and durable job ownership.
5. **Observation and validation:** add push/token transport only with defined
   reconnect/backpressure semantics; expand real-provider, visual and load tests.

These choices keep the loop small and the failure boundaries explicit. SQLite,
event polling and a single lifecycle owner reduce operational complexity now;
they are deliberate limits, not claims of production scale or exactly-once safety.
