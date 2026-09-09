# Minder Agent Harness

A bounded Python agent runtime that accepts tasks over RPC, reasons across
multiple model/tool steps, and records execution in SQLite. The agent loop is
implemented directly, with separate interfaces for the model provider, MCP tools
and persistence.

Gemini is the application's model provider. A separate mock factory server exposes
three read-only MCP tools for machine status, work orders and safety procedures.
Factory data is simulated; factory-specific behavior stays outside the agent core.

![Minder agent harness WebUI](docs\architecture\webui.png)

## What the project demonstrates

- **Inspectable execution:** persistent sessions, ordered events and tool-attempt
  records, exposed through the same API to a CLI and a Simple/Detailed Web UI.
- **Explicit failure handling:** bounded retries, cancellation during async I/O,
  crash recovery and a durable guard against repeating uncertain unsafe tool calls.
- **Replaceable boundaries:** RPC delegates to an application service; the loop
  uses provider/tool protocols. Fake and local-model evaluation reuse that loop.

Read the [architecture](docs\architecture.md) for component ownership,
execution flow, transaction boundaries, failure behavior and tradeoffs.

## Run locally

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/). Run from the repository root.

### Demo without an API key

~~~powershell
uv sync --locked
uv run python examples/phase4_offline_server.py --port 8000
~~~

Open http://127.0.0.1:8000, or run `uv run minder-cli` in a second terminal.
This mode uses a **scripted model**, with actual HTTP, SQLite and a separate MCP
process. It demonstrates runtime behavior, not real-model reasoning quality.

### Use Gemini

Stop the offline server. Create a private `.env` using [.env.example](.env.example),
set `GEMINI_API_KEY`, and choose a function-calling `GEMINI_MODEL` available to
your account. Then run:

~~~powershell
uv run --env-file .env minder-rpc
~~~

Do not commit credentials. The server reads environment variables; `uv --env-file`
loads the file. The same Web UI and CLI work with either server mode.
See the [interface guide](docs/guides/interfaces.md) and
[demo scenarios](docs/guides/rpc.md#demo-and-validation) for success, tool failure,
cancellation and reopening a session after restart.

## Validation and limits

Fault tests exercise real MCP subprocesses, SQLite transactions, cancellation,
connection loss and crash windows. The [testing guide](tests/README.md) explains
how to run them and what each group verifies.

The [local-model report](docs/reports/local-model-test-report.md) records 184
offline tests passing on 9 September 2026 and a separate 22-run evaluation of two
GGUF models. Local inference is evaluation-only, not an application provider or
automatic fallback. Model-quality results are distinct from runtime test results.

The application targets one local process and one database owner, without
authentication. Do not expose it publicly. Progress is delivered by event polling,
not token or private-reasoning streaming. Cancellation cannot undo remote effects,
and execution is not guaranteed exactly once.

## Read more

| Topic | Documentation |
| --- | --- |
| Architecture and engineering decisions | [Architecture one-pager](docs/architecture.md) · [Detailed architecture](docs/architecture/overview.md) · [Short design note](docs/architecture/submission.md) |
| Using and integrating the runtime | [CLI/Web](docs/guides/interfaces.md) · [RPC API](docs/guides/rpc.md) |
| Tool and state boundaries | [MCP lifecycle](docs/guides/mcp.md) · [Persistence and recovery](docs/guides/persistence.md) |
| Evidence and experiments | [Runtime test report](docs/reports/phase-5-test-report.md) · [Local evaluation](docs/reports/local-model-test-report.md) · [Gemini vs local](docs/architecture/gemini-vs-local.md) |
| Scope and development history | [Documentation index](docs/README.md) · [Project brief](docs/requirements/project-brief.md) · [Engineering journal](docs/engineering-journal/README.md) |
