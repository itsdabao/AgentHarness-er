from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from time import monotonic

import anyio
from jsonschema import Draft202012Validator

from ..core.cancellation import OperationCancelled
from ..core.models import JSONValue, RuntimeError, ToolCall, ToolDefinition, ToolResult
from ..core.ports import ReplayBlocked, StorageError, ToolExecutionContext
from .client import classify_client_error
from .contracts import DiscoveredTool, MCPClientBackend, MCPClientError, MCPToolResponse


class MCPToolExecutor:
    """Approved, validated and bounded ToolExecutor backed by MCP."""

    def __init__(
        self,
        client: MCPClientBackend,
        approved_tools: Sequence[str],
        *,
        call_timeout_seconds: float = 2.0,
        max_attempts: int = 2,
        retry_delay_seconds: float = 0.2,
        server_scope: str = "default-local-mcp",
    ) -> None:
        if call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")

        self._client = client
        if not server_scope.strip():
            raise ValueError("server_scope must identify the server and security context")
        self._server_scope = server_scope
        self._approved_tools = frozenset(approved_tools)
        self._call_timeout_seconds = call_timeout_seconds
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._catalog: dict[str, DiscoveredTool] | None = None
        self._catalog_version = -1
        self._uncertain_calls: set[str] = set()

    async def discover_tools(
        self,
        *,
        refresh: bool = False,
        context: ToolExecutionContext | None = None,
    ) -> tuple[ToolDefinition, ...]:
        """Cache the catalog per executor; refresh explicitly when capabilities change."""
        if (
            self._catalog is None
            or refresh
            or self._catalog_version != self._client.catalog_version
        ):
            _check_budget(context)
            timeout = self._call_timeout_seconds
            if context is not None and context.timeout_seconds is not None:
                timeout = min(timeout, context.timeout_seconds)
            discovered = await self._client.list_tools(
                timeout, None if context is None else context.cancellation, refresh=refresh
            )
            self._catalog = {tool.definition.name: tool for tool in discovered}
            self._catalog_version = self._client.catalog_version
        return tuple(
            tool.definition
            for tool in self._catalog.values()
            if tool.definition.name in self._approved_tools
        )

    async def execute(
        self,
        tool_call: ToolCall,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        deadline = (
            None
            if context is None or context.timeout_seconds is None
            else monotonic() + context.timeout_seconds
        )
        if tool_call.name not in self._approved_tools:
            return _failure(
                tool_call,
                "TOOL_NOT_APPROVED",
                f"Tool {tool_call.name!r} is not approved for this runtime.",
            )

        try:
            _check_budget(context, deadline)
            discovery_context = (
                None
                if context is None
                else replace(context, timeout_seconds=_remaining_seconds(deadline))
            )
            await self.discover_tools(context=discovery_context)
            _check_budget(context, deadline)
            discovered = self._validated_tool(tool_call)
        except Exception as exc:
            error = classify_client_error(exc, default_code="MCP_DISCOVERY_ERROR")
            return ToolResult.failure(tool_call, error)

        call_key = _call_key(tool_call)
        if (context is None or context.recorder is None) and call_key in self._uncertain_calls:
            return _failure(
                tool_call,
                "AMBIGUOUS_TOOL_OUTCOME",
                "An earlier identical call may have completed remotely; "
                "automatic replay is blocked.",
            )

        return await self._execute_discovered(
            tool_call, discovered, self._catalog_version, context, call_key, deadline
        )

    def _validated_tool(self, tool_call: ToolCall) -> DiscoveredTool:
        assert self._catalog is not None
        discovered = self._catalog.get(tool_call.name)
        if discovered is None:
            raise MCPClientError(
                "TOOL_NOT_FOUND",
                f"Tool {tool_call.name!r} was not advertised by the MCP server.",
            )
        errors = sorted(
            Draft202012Validator(discovered.definition.parameters).iter_errors(tool_call.arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise MCPClientError(
                "INVALID_TOOL_ARGUMENTS",
                "Tool arguments do not match the advertised schema.",
                details={"validation_errors": [error.message for error in errors]},
            )
        return discovered

    async def _execute_discovered(
        self,
        tool_call: ToolCall,
        discovered: DiscoveredTool,
        catalog_version: int,
        context: ToolExecutionContext | None,
        call_key: str,
        deadline: float | None,
    ) -> ToolResult:
        attempts: list[dict[str, JSONValue]] = []

        for attempt in range(1, self._max_attempts + 1):
            if catalog_version != self._client.catalog_version:
                try:
                    refresh_context = (
                        None
                        if context is None
                        else replace(context, timeout_seconds=_remaining_seconds(deadline))
                    )
                    await self.discover_tools(context=refresh_context)
                    updated = self._validated_tool(tool_call)
                    if attempt > 1 and not updated.retry_safe:
                        raise MCPClientError(
                            "TOOL_RETRY_UNSAFE", "Tool is no longer safe to retry."
                        )
                    discovered = updated
                    catalog_version = self._catalog_version
                except Exception as exc:
                    return ToolResult.failure(
                        tool_call,
                        classify_client_error(exc),
                        metadata=_attempt_metadata(attempts, attempt - 1, self._max_attempts),
                    )
            if context is not None and context.cancellation.is_cancelled:
                return _failure(
                    tool_call,
                    "TOOL_CANCELLED",
                    "Tool execution was cancelled.",
                    metadata=_attempt_metadata(attempts, attempt - 1, self._max_attempts),
                )

            timeout_seconds = _attempt_timeout(self._call_timeout_seconds, deadline)
            if timeout_seconds <= 0:
                return _failure(
                    tool_call,
                    "TOOL_TIMEOUT",
                    "No execution time remains for the tool call.",
                    retryable=True,
                    metadata=_attempt_metadata(attempts, attempt - 1, self._max_attempts),
                )

            if attempt > 1:
                await _emit_retry_started(context, tool_call, attempt, self._max_attempts)

            recorder = None if context is None else context.recorder
            attempt_id = None
            if recorder is not None:
                try:
                    attempt_id = await recorder.begin_attempt(
                        tool_call,
                        self._server_scope,
                        attempt,
                        discovered.retry_safe,
                    )
                except ReplayBlocked as exc:
                    return _failure(tool_call, "AMBIGUOUS_TOOL_OUTCOME", str(exc))
                except OperationCancelled as exc:
                    return _failure(tool_call, "TOOL_CANCELLED", str(exc))
                # Recording can consume time or race cancellation. Recheck before dispatch.
                try:
                    _check_budget(context, deadline)
                except Exception as exc:
                    result = ToolResult.failure(tool_call, classify_client_error(exc))
                    await recorder.end_attempt(attempt_id, result, False)
                    return result
                timeout_seconds = _attempt_timeout(self._call_timeout_seconds, deadline)

            try:
                response = await self._client.call_tool(
                    tool_call.name,
                    tool_call.arguments,
                    timeout_seconds,
                    None if context is None else context.cancellation,
                    expected_catalog_version=catalog_version,
                )
                if not isinstance(response, MCPToolResponse):
                    raise MCPClientError(
                        "MCP_MALFORMED_RESPONSE",
                        "The MCP client returned an invalid tool result.",
                    )
            except StorageError:
                raise
            except Exception as exc:
                error = classify_client_error(exc)
                # The SDK marks failures before dispatch as False. Unknown backends
                # are treated conservatively once call_tool has been entered.
                outcome_unknown = error.details.get("outcome_unknown", True) is not False
                error = replace(error, details=error.details | {"outcome_unknown": outcome_unknown})
                if outcome_unknown and not discovered.retry_safe:
                    self._uncertain_calls.add(call_key)
                if recorder is not None and attempt_id is not None:
                    await recorder.end_attempt(
                        attempt_id,
                        ToolResult.failure(tool_call, error),
                        outcome_unknown,
                    )
                attempts.append(
                    {
                        "attempt": attempt,
                        "tool_call_id": tool_call.tool_call_id,
                        "status": "failed",
                        "error_code": error.code,
                    }
                )
                if self._can_retry(error, discovered, attempt, deadline, context):
                    await _emit_retry_failure(
                        context,
                        tool_call,
                        attempt,
                        self._max_attempts,
                        error,
                        self._retry_delay_seconds,
                    )
                    if await _wait_for_retry(context, self._retry_delay_seconds):
                        return _failure(
                            tool_call,
                            "TOOL_CANCELLED",
                            "Tool execution was cancelled during retry waiting.",
                            metadata=_attempt_metadata(attempts, attempt, self._max_attempts),
                        )
                    continue
                return ToolResult.failure(
                    tool_call,
                    error,
                    metadata=_attempt_metadata(attempts, attempt, self._max_attempts),
                )
            except BaseException:
                # A task-level cancellation cannot return a ToolResult; keep the local
                # replay guard before propagating it to the application lifecycle.
                if not discovered.retry_safe:
                    self._uncertain_calls.add(call_key)
                raise

            attempts.append(
                {
                    "attempt": attempt,
                    "tool_call_id": tool_call.tool_call_id,
                    "status": "failed" if response.is_error else "succeeded",
                    **({"error_code": "MCP_TOOL_ERROR"} if response.is_error else {}),
                }
            )
            metadata = _attempt_metadata(attempts, attempt, self._max_attempts)
            result = (
                _failure(
                    tool_call,
                    "MCP_TOOL_ERROR",
                    response.error_message or "The MCP tool returned an error.",
                    model_content=response.model_content,
                    metadata=metadata,
                )
                if response.is_error
                else ToolResult.success(tool_call, response.model_content, metadata)
            )
            if recorder is not None and attempt_id is not None:
                await recorder.end_attempt(attempt_id, result, False)
            return result

        raise AssertionError("unreachable retry loop")

    def _can_retry(
        self,
        error: RuntimeError,
        discovered: DiscoveredTool,
        attempt: int,
        deadline: float | None,
        context: ToolExecutionContext | None,
    ) -> bool:
        if not error.retryable or not discovered.retry_safe:
            return False
        if attempt >= self._max_attempts:
            return False
        if context is not None and context.cancellation.is_cancelled:
            return False
        remaining = _remaining_seconds(deadline)
        return remaining is None or remaining > self._retry_delay_seconds


def _check_budget(context: ToolExecutionContext | None, deadline: float | None = None) -> None:
    if context is None:
        return
    if context.cancellation.is_cancelled:
        raise MCPClientError("TOOL_CANCELLED", "Tool execution was cancelled.")
    remaining = context.timeout_seconds if deadline is None else _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise MCPClientError("TOOL_TIMEOUT", "No execution time remains for the tool call.")


def _failure(
    tool_call: ToolCall,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, JSONValue] | None = None,
    model_content: JSONValue | None = None,
    metadata: dict[str, JSONValue] | None = None,
) -> ToolResult:
    return ToolResult.failure(
        tool_call,
        RuntimeError(
            code=code,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
        ),
        model_content=model_content,
        metadata=metadata,
    )


def _attempt_metadata(
    attempts: list[dict[str, JSONValue]],
    attempt_count: int,
    max_attempts: int,
) -> dict[str, JSONValue]:
    return {
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "attempts": list(attempts),
    }


def _call_key(tool_call: ToolCall) -> str:
    return f"{tool_call.name}:{json.dumps(tool_call.arguments, sort_keys=True)}"


def _attempt_timeout(call_timeout_seconds: float, deadline: float | None) -> float:
    remaining = _remaining_seconds(deadline)
    return call_timeout_seconds if remaining is None else min(call_timeout_seconds, remaining)


def _remaining_seconds(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - monotonic())


async def _wait_for_retry(
    context: ToolExecutionContext | None,
    delay_seconds: float,
) -> bool:
    if context is None:
        if delay_seconds:
            await anyio.sleep(delay_seconds)
        return False
    return await context.cancellation.wait(delay_seconds)


async def _emit_retry_failure(
    context: ToolExecutionContext | None,
    tool_call: ToolCall,
    attempt: int,
    max_attempts: int,
    error: RuntimeError,
    retry_delay_seconds: float,
) -> None:
    if context is None:
        return
    await context.emit(
        "tool_execution_failed",
        {
            "tool_call_id": tool_call.tool_call_id,
            "tool_name": tool_call.name,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_code": error.code,
            "retry_reason": error.code,
            "will_retry": True,
            "retry_delay_ms": int(retry_delay_seconds * 1000),
        },
    )


async def _emit_retry_started(
    context: ToolExecutionContext | None,
    tool_call: ToolCall,
    attempt: int,
    max_attempts: int,
) -> None:
    if context is None:
        return
    await context.emit(
        "tool_execution_started",
        {
            "tool_call_id": tool_call.tool_call_id,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
    )
