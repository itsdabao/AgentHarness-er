# Minder Agent Harness — Architecture

Minder is a bounded, asynchronous agent runtime. A caller submits a task through
an HTTP RPC-style API; the runtime asks an LLM what to do, executes only approved
MCP tools, records every transition, and returns an inspectable run. The factory
tools are a separate mock MCP server, so the agent core does not depend on factory
business rules.

## Pipeline

![Minder agent harness pipeline](architecture/architecture-pipeline.png)

*The static diagram shows the runtime boundary and the model/tool feedback path.*

The arrows represent calls or communication; SQLite receives trace data through
interfaces rather than being imported by the loop. The important feedback edge is
`tool result → loop → next model request`: the model can use an earlier result to
choose a later action. The LLM never calls a tool directly.

## Boundaries and responsibilities

| Boundary | Responsibility | Reason for the boundary |
| --- | --- | --- |
| RPC adapter | Validate envelopes, map protocol errors, expose submit/status/cancel/history | Transport can be replaced without changing agent execution |
| `AgentService` | Own sessions, active tasks, admission, recovery and shutdown | One place owns lifecycle and prevents two active runs in one session |
| `AgentHarness` / `AgentLoop` | Enforce steps, tool-call, deadline and cancellation limits; coordinate model/tool/result iterations | Keeps policy and control flow independent of HTTP, SQL and SDKs |
| Provider adapter | Translate the provider wire format and apply bounded provider retries | Gemini-specific details stay out of core types |
| MCP executor | Check allowlist/catalog/schema, create attempt identities, call tools and record outcomes | Tool authority and external side effects have one choke point |
| `ExecutionStore` | Persist sessions, messages, runs, events, task state and attempts | A run can be resumed or audited after the process exits |
| Mock MCP server | Provide small read-only factory tools over a separate process | Integration failures can be tested without touching real equipment |

The core depends on small async ports (`LLMProvider`, `ToolExecutor`, event and
attempt-recording protocols). This is deliberate: an in-process fake provider can
exercise the same loop as Gemini, and a different RPC transport can reuse the same
service.

## One run, including failure paths

1. The service commits a queued run, user message and initial event, then returns a
   run snapshot while the background task continues.
2. Before each model/tool step, the loop checks cancellation, deadline and budgets.
3. The provider returns a final answer or validated tool calls. Tool calls are
   executed serially; their results become the next model input.
4. MCP retries are bounded adapter attempts, only for explicitly retryable and
   replay-safe failures. A tool error is normally fed back to the model; a storage
   error fails closed and stops further work.
5. The store commits the authoritative terminal status and event. `completed`
   means a final answer was accepted, not that its business correctness was proven.

Cancellation is cooperative: it stops work at safe boundaries, but cannot undo a
remote tool that has already completed. SQLite transactions are short and do not
span external I/O; an unresolved attempt is not automatically replayed after a
crash.

## What I would change with more time

The next experiment I would run is deliberately model-focused: send the same
multi-step and failure scenarios through Gemini and small local models (SLMs), then
compare tool-call validity, completion rate, latency, context usage and recovery
behavior. This tests whether the harness remains safe and useful when the model is
weaker, instead of hiding model quality behind a happy-path demo. If a local SLM is
good enough for bounded factory tasks, it can reduce API cost, improve privacy and
make offline evaluation practical; harder tasks can still use a hosted model under
an explicit routing policy.

If I had the opportunity to work on Minder beyond this project, I would turn the
baseline into a concrete operational workflow with real data and operator feedback.
Once that scope was stable, I would study state-of-the-art harness systems, such as
Anthropic's Claude Code, alongside current work on durable execution, context
management, tool orchestration and agent safety. I would bring selected ideas back
as small, measurable experiments against Minder's traces and failure scenarios—not
copy a framework wholesale. That would give the architecture a real workload,
clear constraints and an evidence-based way to evolve its boundaries while keeping
the core loop simple.

For the full component inventory and implementation references, see the [detailed
architecture overview](architecture/overview.md), [RPC guide](guides/rpc.md), and
[persistence guide](guides/persistence.md).
