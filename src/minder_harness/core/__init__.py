"""Transport-independent contracts and execution primitives."""

from .cancellation import CancellationToken
from .debug import render_event_trace
from .events import InMemoryEventCollector
from .harness import AgentHarness
from .loop import AgentLoop, LoopResult
from .models import (
    Event,
    ExecutionLimits,
    Message,
    ModelResponse,
    Run,
    RuntimeError,
    Session,
    ToolCall,
    ToolDefinition,
    ToolResult,
    Usage,
)
from .ports import EventEmitter, LLMProvider, ToolExecutionContext, ToolExecutor

__all__ = [
    "Event",
    "EventEmitter",
    "ExecutionLimits",
    "AgentHarness",
    "AgentLoop",
    "CancellationToken",
    "InMemoryEventCollector",
    "LLMProvider",
    "LoopResult",
    "Message",
    "ModelResponse",
    "Run",
    "RuntimeError",
    "Session",
    "ToolCall",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolResult",
    "Usage",
    "render_event_trace",
]
