# Agent Harness Project Brief

## Purpose

Build a small, generic, bounded agent runtime for the Celesnity AI Track
assessment.

Prioritize a working end-to-end system, clear architectural boundaries,
inspectable execution, and explicit failure handling.

Factory-specific behavior belongs in demo tools, not in the runtime core.

## How to use this document

This document describes the overall project. It does not authorize
implementing every feature at once.

The active phase prompt determines what to implement now.
Requirements outside that phase remain deferred.

The assessment defines submission requirements. Proposed approaches below
are implementation guidance and may change with documented trade-offs.

## Assessment requirements

The completed system should provide:

- A thin RPC-style API for managing sessions, submitting messages or tasks,
  and checking run status.
- A custom agent loop that supports multiple LLM and tool-call steps.
- Tool results fed back into the loop.
- MCP integration, with a separate mock server exposing two or three tools
  as requested by the assessment.
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

0. Project skeleton and development tooling.
1. Core contracts, a pure bounded loop, and a minimal in-memory Harness with
   deterministic test doubles.
2. MCP integration and a separate mock tool server.
3. Persistence, resume, and execution lifecycle handling.
4. RPC integration, real-provider execution, streaming, and demo validation.

This sequence may change to support an earlier end-to-end demonstration.
Each phase prompt must state its own scope and acceptance criteria.

Concrete RPC transport, MCP transport, persistence technology and schema, real
LLM provider, exact execution budgets, and detailed termination policies remain
deferred until their relevant phases. Their architectural boundaries are not
deferred: RPC remains outside the loop, MCP remains behind `ToolExecutor`, model
providers remain behind `LLMProvider`, and persistence remains outside reasoning
logic.

## Scope discipline

Avoid adding UI, RAG, multi-agent orchestration, additional providers,
or infrastructure unless needed to satisfy the assessment.

Prefer a small working implementation with clear limitations over
speculative abstractions.

## Completion criteria

The project is ready for submission when the required behavior can be
demonstrated, setup instructions are reproducible, meaningful checks have
been run, and remaining limitations are documented.
