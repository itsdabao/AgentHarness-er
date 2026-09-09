from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable

from minder_harness.core import (
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
)
from minder_harness.core.ports import ModelExecutionContext


class FakeLLMProvider:
    def __init__(
        self,
        responses: Sequence[ModelResponse] = (),
        always: ModelResponse | None = None,
        on_generate: Callable[[int], Awaitable[None] | None] | None = None,
        respond: Callable[[Sequence[Message]], ModelResponse] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._always = always
        self._on_generate = on_generate
        self._respond = respond
        self.calls: list[tuple[Message, ...]] = []
        self.tool_definitions: list[tuple[ToolDefinition, ...]] = []

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        context: ModelExecutionContext | None = None,
    ) -> ModelResponse:
        self.calls.append(tuple(messages))
        self.tool_definitions.append(tuple(tools))
        if self._on_generate is not None:
            pending = self._on_generate(len(self.calls))
            if isawaitable(pending):
                await pending
        if self._respond is not None:
            return self._respond(messages)
        if self._always is not None:
            return self._always
        if not self._responses:
            raise AssertionError("FakeLLMProvider has no scripted response")
        return self._responses.pop(0)


class FakeToolExecutor(ToolExecutor):
    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.results = {} if results is None else results
        self.calls: list[ToolCall] = []

    async def execute(
        self,
        tool_call: ToolCall,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.calls.append(tool_call)
        try:
            return self.results[tool_call.tool_call_id]
        except KeyError as exc:
            raise AssertionError(f"No fake result for {tool_call.tool_call_id}") from exc
