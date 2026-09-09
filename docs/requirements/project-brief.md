# Agent Harness Project Brief

## Purpose

Build a small, generic, bounded agent runtime that a platform can drive over RPC,
with approved MCP tools and persistent, inspectable execution.

Prioritize a working end-to-end system, clear architectural boundaries,
inspectable execution, and explicit failure handling.

Factory-specific behavior belongs in demo tools, not in the runtime core.

## How to use this document

This document describes the overall project. It does not authorize
implementing every feature at once.

The active phase prompt determines what to implement now.
Requirements outside that phase remain deferred.

The project originated from an assessment, retained in [assessment.docx](assessment.docx)
as a historical reference. This brief defines the project's scope; proposed
approaches may change with documented trade-offs.

## Core requirements

The completed system should provide:

- A thin RPC-style API for managing sessions, submitting messages or tasks,
  and checking run status.
- A custom agent loop that supports multiple LLM and tool-call steps.
- Tool results fed back into the loop.
- MCP integration, with a separate mock server exposing three demo tools.
- Core types, events, and inspectable execution state.
- Persistent sessions that can be resumed.
- Stored tool calls and events for traceability.
- One real LLM provider.
- Explicit handling of tool failures, unknown tools, slow MCP responses,
  and connection loss.

The demo should show:

- Submitting a task and observing progress and tool calls as they occur.
- Cancelling an active run and showing it stop cleanly.
- A failure scenario and the resulting runtime behavior.

A second mock LLM provider is optional.

Deliverables include a repository and a short architecture writeup
explaining boundaries, trade-offs, limitations, and future improvements.

## Architectural direction

- Keep RPC transport separate from the agent core.
- Keep provider-specific SDK structures behind an LLM interface.
- Route agent-invoked business tools through the controlled MCP boundary.
- Keep the mock MCP server separate from the runtime.
- Represent execution state and events explicitly.
- Keep factory-specific concepts outside the generic core.

Architecture diagrams describe logical responsibilities. They do not
require a separate class or module for every layer.

### High-level architecture

```text
External Platform
        |
        v
RPC Adapter
        |
        v
Application / Agent Service
        |
        v
Agent Harness
        |
        v
Agent Loop
     /        \
LLMProvider   ToolExecutor
                  |
                  v
                 MCP
                  |
                  v
              MCP Server
```

The Agent Loop is the small execution mechanism. It requests the next
provider-neutral model step, executes requested tools through `ToolExecutor`,
feeds each `ToolResult` into a later model step, and repeats until it reaches a
terminal outcome, cancellation, or an execution limit.

The Agent Harness controls an execution around the loop. It owns Run lifecycle,
inspectable execution state, cancellation signals, execution limits, and event
coordination. It does not make provider- or tool-specific business decisions.

The Agent Loop must not own or directly depend on RPC transport, persistence
technology, MCP SDK details, provider SDK details, factory-specific logic, or
UI and console presentation. Infrastructure implements or adapts to boundaries
defined by the core; dependencies point inward rather than importing
infrastructure into the loop.

### Domain distinctions

- A Session is a persistent interaction and context container; a Run is one
  execution attempt inside a Session.
- A Message participates in the model transcript; an Event records a runtime
  fact about execution.
- A ToolCall is a requested operation; a ToolResult is its normalized outcome.
- AgentHarness manages execution; AgentLoop performs the bounded reasoning and
  tool-use cycle.

### Structured execution and events

Internal core models should be typed. RPC payloads and persisted records must
be JSON-serializable. Provider-specific and MCP-specific payloads are normalized
at their adapters before entering core logic; raw SDK objects and exceptions do
not become Agent Loop contracts.

The Loop and Harness produce a deliberately small set of structured execution
events. Persistence, streaming, logging, and presentation consume those events.
The Agent Loop does not save events to a concrete database, send HTTP streams,
print execution state, or depend on presentation logic.

In Phase 1, events remain in an ordered in-memory collection. In-memory does
not mean invisible: tests inspect the event list directly, and an optional
developer-only renderer outside the loop can show the current trace in the
terminal or failed-test diagnostics. Rendering does not change execution or
save event history. Events disappear when the process exits; their
JSON-serializable contract is reused for durable storage in Phase 3 and
RPC/event streaming in Phase 4.

Progress exposed to callers means execution progress, model-step lifecycle,
tool calls, tool results, errors, status transitions, and the final answer. It
does not include private chain-of-thought.

### Cancellation and bounded execution

Cancellation is a first-class lifecycle behavior:

```text
RUNNING -> CANCEL_REQUESTED -> CANCELLED
```

A cancellation signal eventually propagates to the Agent Loop, an active LLM
request where supported, and an active ToolExecutor or MCP operation where
supported. A late result after cancellation may be recorded for diagnostics but
must not be fed back to the model or schedule new work.

Every Run is bounded by configurable concepts such as `max_steps`,
`max_tool_calls`, an overall deadline or timeout, and bounded retries. Exact
numeric defaults are chosen in the relevant implementation phase, not in this
brief. Exhausting a budget produces an explicit terminal limit-exceeded outcome.

### Failure model

Relevant phases must normalize and handle provider unavailability, timeout,
authentication failure, rate limiting, malformed provider output, invalid
model-generated tool calls, unknown or unapproved tools, invalid tool
arguments, tool failure or timeout, MCP unavailability or disconnection,
malformed MCP responses, cancellation, execution-limit exhaustion, and process
interruption. These behaviors are introduced incrementally rather than all in
the foundation phase. Retries, where used, are failure-specific and bounded.

## Proposed execution policies

Define these policies when implementing the relevant functionality:

- Run limits and termination conditions.
- Approved tools and tool-argument validation.
- Tool timeouts and bounded retry behavior.
- Cancellation and handling of in-flight operations.
- Session resume semantics and interrupted-run behavior.
- Event ordering and persistence guarantees.
- Provider error normalization and malformed-response handling.

Document the selected behavior and its limitations. Do not claim guarantees
that the implementation cannot provide.

## Testing strategy

Add tests alongside implemented behavior. Do not create placeholder tests
for future components.

Use deterministic fake providers and controlled MCP fixtures where useful.

Prioritize:

- A multi-step task that consumes a tool result before finishing.
- Tool failure and unknown-tool handling.
- Slow or disconnected MCP servers.
- Run limits that prevent unbounded execution.
- Cancellation that prevents further work from being scheduled.
- Session persistence and resume according to documented semantics.
- Stored events and tool calls sufficient to inspect a run.

Keep real-provider smoke checks separate from deterministic automated tests.
Document required credentials and report checks that were not run.

## Proposed development sequence

- Phase 0: Project skeleton and development tooling.
- Phase 1: Core contracts, a pure bounded loop, and a minimal in-memory Harness with
   deterministic test doubles.
- Phase 2: MCP integration and a separate mock tool server.
- Phase 3: Persistence, resume, and execution lifecycle handling.
- Phase 4: RPC integration, real-provider execution, streaming, and demo validation.
- Phase 4.5: Optional [terminal control and inspection client](../phases/phase-4.5-terminal-interface.json)
  over the Phase 4 RPC API, supporting task submission, progress, cancel and trace inspection.
- Phase 5: Implemented optional bonus: reusable test runtime, database lifecycle/fault
   cases and dependent multi-tool integration scenarios. See
   [Phase 5 scope and evidence](../phases/phase-5-bonus-test-support.json) and
   [test coverage guide](../../tests/README.md). Live-model reasoning evaluation is separate.

Recommended order: Phase 3 -> Phase 4 -> Phase 4.5 -> Phase 5. The terminal client
needs the Phase 4 RPC contract; its design can be prepared earlier, but completion
must exercise that transport. Phase 4 stays independently demonstrable.
Phases 4.5 and 5 are optional project improvements, not additional assessment
requirements. Phase 5 may proceed without 4.5 if the optional client is skipped.

This sequence may change to support an earlier end-to-end demonstration.
Each phase prompt must state its own scope and acceptance criteria.

Concrete RPC transport, MCP transport, persistence technology and schema, real
LLM provider, exact execution budgets, and detailed termination policies remain
deferred until their relevant phases. Their architectural boundaries are not
deferred: RPC remains outside the loop, MCP remains behind `ToolExecutor`, model
providers remain behind `LLMProvider`, and persistence remains outside reasoning
logic.

## Scope discipline

### Keep implementation clean

- Prefer straightforward control flow, descriptive names, and focused functions
  that can be understood from top to bottom.
- Check existing helpers, core contracts, standard-library functions, and SDK
  capabilities before adding code; reuse them when they fit.
- Extract helpers for meaningful duplication or clarity. Avoid forwarding-only
  layers, speculative base classes, and a class for every logical boundary.
- Add modules, interfaces, dependencies, and configuration only when the active
  phase needs them. Keep failure handling explicit rather than compressing code.
- Reuse SDK timeout/retry support when it satisfies budgets, cancellation, and
  attempt visibility; otherwise use a small adapter helper, not a retry framework.

### Retry and cancellation ownership by phase

- Phase 0 documents boundaries without adding runtime machinery.
- Phase 1 introduced scheduling checkpoints and no automatic loop retry;
  provider errors fail the run and tool errors become model input.
- Phase 2 upgrades core I/O to async and adds a long-lived MCP session with
  startup/discovery before runs, versioned schema reuse, bounded reconnect,
  per-call timeouts and cancellable retry waits. Cancellation is per request;
  shutdown/connection loss interrupts pending I/O. The in-memory harness rejects
  overlapping runs per session and checks cancel/deadline before accepting output.
  Async I/O must yield; arbitrary synchronous code cannot be forcibly interrupted.
- Phase 3 persists cancellation, attempts and uncertain outcomes, extends the
  existing late-result/concurrency guards to durable lifecycle transitions,
  and marks unfinished runs interrupted on restart.
- Phase 4 connects these rules to the real provider and RPC cancellation demo.
  Distributed workers and cross-process coordination remain deferred.

Adapters own automatic transport retries. Require a classified transient error,
safe repeat execution, available attempts, and remaining run time; `retryable=true`
alone is insufficient. Unknown errors default to non-retryable. Validation and
authentication failures are not transient. Do not add automatic business retries
to AgentLoop or a generic idempotency framework.

Use one retry owner per operation so SDK and wrapper retries do not multiply.
`max_attempts` includes the first request. Start with a short bounded delay;
capped exponential backoff and jitter are optional refinements. Trace every
actual attempt and outcome through the existing Event contract. Adapter attempts
belong to one logical model step/tool call and do not inflate core usage counters;
their attempts and waits all consume the same overall deadline.

A model requesting a tool again creates a new logical call and consumes loop
budgets. It must not bypass protection against replay of an ambiguous
side-effecting operation. Stopping local waiting does not prove a remote tool
stopped or undo its effects. Document these limits in the demo.

Presentation work is limited to the optional Phase 4.5 terminal RPC client.
Avoid adding other UI, RAG, multi-agent orchestration, additional providers,
or infrastructure unless needed to satisfy the assessment.

Prefer a small working implementation with clear limitations over
speculative abstractions.

## Completion criteria

The project is ready for submission when the required behavior can be
demonstrated, setup instructions are reproducible, meaningful checks have
been run, and remaining limitations are documented.
