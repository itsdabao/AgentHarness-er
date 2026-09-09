# Phase 4 RPC and provider guide

The server is a local, single-worker JSON-RPC 2.0 subset over HTTP. It exposes the
existing AgentService and starts a separate mock MCP process. Event polling provides
incremental progress; this phase does not implement token streaming or the Phase 4.5
interactive terminal.

## Start and configure

Run `uv sync --locked`. In PowerShell, set environment variables in the terminal
that starts the server:

~~~powershell
$env:GEMINI_API_KEY = '<your-key>'
$env:GEMINI_MODEL = 'gemini-2.5-flash'
uv run minder-rpc --port 8000
~~~

Do not commit the key. The application reads the process environment; it does not
automatically load .env. Bind is deliberately 127.0.0.1 and there is no authentication
or multi-user support. Do not expose it to a public network or start multiple workers.

| Variable | Default | Meaning |
| --- | --- | --- |
| GEMINI_API_KEY | required | Provider credential; sent in header, never URL |
| GEMINI_MODEL | gemini-2.5-flash | Server-selected model; no fallback |
| MINDER_DATABASE | var/harness.db | Durable SQLite file |
| MINDER_PROVIDER_MAX_ATTEMPTS | 2 | Includes first request; 1 disables retry |
| MINDER_PROVIDER_TIMEOUT | 30 | Per-attempt seconds, clipped to run deadline |
| MINDER_INPUT_BUDGET | 32768 | Combined conservative input estimate + output headroom |
| MINDER_OUTPUT_TOKENS | 2048 | Generation output cap and reserved headroom |

The documented model supports function calling; changing it requires verifying model
availability, supported settings and continuation compatibility. Old provider
continuation metadata from another model is rejected explicitly.

Startup verifies the required MCP tool catalog before serving requests. GET /health
returns an ok/data/error envelope containing ready. During admission failure it
returns HTTP 503. This is local readiness, not a guarantee of Gemini availability.

## RPC contract

POST /rpc accepts one JSON-RPC request with named params. Batch calls and positional
params are unsupported. Requests have a 64 KiB limit and a five-second body-read
deadline. Callers should use IDs for mutations; notifications execute but return
HTTP 204 without a result.

~~~json
{"jsonrpc":"2.0","id":1,"method":"create_session","params":{}}
~~~

~~~json
{"jsonrpc":"2.0","id":1,"result":{"ok":true,"data":{"session_id":"ses_example","created_at":"..."},"error":null}}
~~~

Domain failures are operation results with ok=false, data=null and an error object
containing code/message/retryable/details. Protocol errors instead use the standard
outer error member (-32700 parse, -32600 invalid request, -32601 method, -32602 params).
The two members result and error never coexist at the outer level.

| Method | Params | Result data |
| --- | --- | --- |
| create_session | optional metadata object | session_id, created_at |
| get_session | session_id | metadata, timestamps, message_count; no full transcript |
| list_sessions | before=null, limit=20 (1-100) | sessions, next_before, has_more |
| submit_task | session_id, content string, optional constraints and limits | Queued run snapshot immediately |
| get_run | run_id | Lightweight status, output/error, usage, timestamps; no events |
| list_run_events | run_id, after_sequence=0, limit=100 | events, next_after_sequence, has_more |
| cancel_run | run_id | Latest acknowledged run snapshot; idempotent |

Event page size is 1-500. Content is a nonempty string of at most 16000 characters.

Session listing is ordered newest-created first with an exclusive insertion-order
cursor. Pass next_before as before for the next page; active run updates do not
reorder pages. before must be a positive safe JSON integer (at most 2^53-1).
Each summary contains session_id, title (first accepted user task, at most 80
characters), created_at, updated_at, latest_run_id and latest_run_status (nullable).
Empty sessions get a fixed placeholder title. No messages, events or provider
continuation are loaded into the response. This read-only API uses the existing
public envelope/redaction. It does not create, replay or cancel a run.

Limits default to max_steps=8, max_tool_calls=8, timeout_seconds=120. RPC caps are
100 steps, 100 tool calls, 300 seconds. The core may support broader JSON values;
the public task API deliberately starts with text input.

Domain error codes include NOT_FOUND, CONFLICT (session already active),
VALIDATION_ERROR, RUNTIME_UNAVAILABLE and INTERNAL_ERROR. Run-level provider/tool
failures appear in a successfully retrieved run snapshot, not as an RPC transport
failure. Public responses omit internal projection fields and provider continuation;
configured credentials are redacted.

## Progress, cancellation and reconnect

Poll status/events approximately every 500 ms. Keep a per-run sequence cursor and
drain pages while has_more is true. After confirming a terminal status, drain the
remaining committed events before ending observation. Reconnect with the last cursor.
get_run never includes the full history.

progress.py uses fixed templates over real event types and error codes. Provider
attempt events reuse model_request_started/model_response_received and carry
record_type=provider_attempt, model/provider, attempt and phase. A failed attempt
records retry_scheduled/retry_reason/retry_delay_ms before waiting. Logical model
steps and HTTP attempts are distinct; the store does not increment model_steps
for attempt records. No model-written loading text or private reasoning is used.

cancel_run records cancellation and signals active I/O/retry waits. Acknowledgment
may still say cancel_requested. Poll until the persisted state is terminal. If
completion won the race, show completed. Cancellation stops local work; it cannot
prove a remote tool stopped or undo its effects. Stopping polling/client disconnect
does not cancel a run. Server shutdown stops admission, cancels/drains runs and
then closes shared MCP, HTTP and SQLite resources.

Never automatically repeat submit_task after losing its response: the run may
already exist. This version has no submit idempotency key or run-list/search API,
so recovering an unknown run ID requires operator inspection of local traces.

## Provider policy and budget

HTTPX performs requests directly; there is no SDK function-execution loop or hidden
retry layer. Retry at most once on timeout, connection loss or 5xx. For 429 require
a valid Retry-After or google.rpc.RetryInfo delay that fits the remaining deadline.
A bare 429 might mean depleted quota: return PROVIDER_RATE_LIMITED, do not guess
its subtype or switch providers. Invalid credentials, permissions, model/request,
malformed output and unknown errors do not retry. Cancellation/deadline override
retry plans. Failed requests can still incur provider charges.

The provider checks the complete serialized request (instructions, transcript,
tool schemas and continuation), estimating one token per UTF-8 byte plus 1024
overhead and reserving the configured output cap. This deliberately conservative
text-only heuristic is NOT an exact tokenizer guarantee. Oversized inputs fail
before HTTP dispatch. Gemini 2.5 Flash thinking is disabled; private thought text
is discarded if returned. Non-thought parts and opaque signatures are retained
only for faithful continuation and filtered from the public RPC view.

Usage.input_tokens/output_tokens sum usage reported by accepted successful model
steps. Attempt events record observed successful-response usage even when later
cancelled. Missing usage and failed/ambiguous request charges are not known; these
counters are not a billing estimate.

## Demo and validation

In a second terminal:

~~~powershell
uv run python examples/phase4_demo.py --scenario success
uv run python examples/phase4_demo.py --scenario failure
uv run python examples/phase4_demo.py --scenario cancel
uv run python examples/phase4_demo.py --session <session_id>
uv run python examples/phase4_demo.py --run <run_id> --json
~~~

Failure asks for an unknown machine. Cancel asks for a slow read and sends cancel
when tool-start is observed (or after three seconds waiting for the model).
Real model tool choices vary; inspect the trace rather than assuming the prompt
guarantees a particular invocation. Restart the server using the same database,
then inspect known IDs or submit a new run in an existing session.

Without credentials use the explicitly FAKE-model server, on the same RPC surface:

~~~powershell
uv run python examples/phase4_offline_server.py --port 8000
~~~

It still uses actual HTTP, SQLite and a separate stdio MCP process. The socket
test runs all three demo scenarios; it does not establish live Gemini behavior.

Optional context evaluation:
`uv run python examples/phase4_context_eval.py` defaults to not_evaluated.
Adding --run with GEMINI_API_KEY permits at most six real requests (one attempt
each, output cap 256). It compares full/selected context at three fact positions,
with distraction, stale observations and a resumed tool exchange. It reports
fixture/model/settings, request size, observed usage, latency and a narrow exact
tool-call/constraint check. One sample per case is exploratory, not evidence that
JSON cures lost-in-the-middle; over-budget inputs are marked unsupported.

Reference implementation sources: [Gemini generateContent](https://ai.google.dev/api/generate-content),
[Gemini model](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash),
[provider troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting),
[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).
