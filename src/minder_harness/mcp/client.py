from __future__ import annotations

import asyncio
import errno
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast

import anyio
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, CallToolResult

from ..core.cancellation import CancellationToken, OperationCancelled, cancellable
from ..core.models import JSONValue, RuntimeError, ToolDefinition, as_jsonable
from .contracts import DiscoveredTool, MCPClientError, MCPToolResponse


class SDKMCPClient:
    """Application-scoped async MCP session. Use with 'async with' before accepting runs."""

    def __init__(
        self, target: object, *, startup_timeout_seconds: float = 5.0, max_in_flight: int = 8
    ) -> None:
        if startup_timeout_seconds <= 0 or max_in_flight < 1:
            raise ValueError("startup timeout and max_in_flight must be positive")
        self._target = target
        self._startup_timeout = startup_timeout_seconds
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_in_flight)
        self._owner: asyncio.Task[None] | None = None
        self._client: Client | None = None
        self._catalog: tuple[DiscoveredTool, ...] = ()
        self._version = 0
        self._closed = True
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._lifecycle_scope: anyio.CancelScope | None = None
        self._error: MCPClientError | None = None

    @property
    def catalog_version(self) -> int:
        return self._version

    @property
    def is_ready(self) -> bool:
        return not self._closed and self._client is not None and not self._stop.is_set()

    async def __aenter__(self) -> SDKMCPClient:
        if not self._closed:
            raise ValueError("MCP client is already open")
        self._closed = False
        try:
            await self.list_tools(self._startup_timeout)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        self._stop.set()
        if self._lifecycle_scope is not None:
            self._lifecycle_scope.cancel()
        # The owner exits its own SDK context; caller tasks never close its cancel scopes.
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if self._owner is not None:
                    await asyncio.shield(self._owner)
                    self._owner = None

    async def list_tools(
        self,
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        refresh: bool = False,
    ) -> tuple[DiscoveredTool, ...]:
        refreshing_client: Client | None = None

        async def discover() -> tuple[DiscoveredTool, ...]:
            nonlocal refreshing_client
            async with self._lock:
                was_ready = self.is_ready
                await self._connect_locked()
                assert self._client is not None
                if refresh and was_ready:
                    client = self._client
                    refreshing_client = client
                    try:
                        catalog = await self._fetch_catalog(client)
                    except Exception as exc:
                        if classify_client_error(exc).code == "MCP_CONNECTION_LOST":
                            self._invalidate(client)
                        raise
                    self._catalog = catalog
                    self._version += 1
                return self._catalog

        return await _bounded(
            discover,
            timeout_seconds,
            cancellation,
            interruption=lambda: self._connection_error(refreshing_client),
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JSONValue],
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        expected_catalog_version: int | None = None,
    ) -> MCPToolResponse:
        dispatched = False
        client: Client | None = None

        async def invoke() -> MCPToolResponse:
            nonlocal dispatched, client
            # Waiting for capacity/connection lock consumes this request's timeout.
            async with self._slots:
                async with self._lock:
                    if not self.is_ready:
                        raise MCPClientError(
                            "MCP_CONNECTION_LOST", "MCP connection is not ready.", retryable=True
                        )
                    if (
                        expected_catalog_version is not None
                        and expected_catalog_version != self._version
                    ):
                        raise MCPClientError(
                            "MCP_CATALOG_CHANGED",
                            "Rediscover tools before dispatch.",
                            retryable=True,
                        )
                    client = self._client
                assert client is not None
                dispatched = True
                result = await client.call_tool(
                    name, arguments, read_timeout_seconds=timeout_seconds
                )
                return _normalize_result(result)

        try:
            return await _bounded(
                invoke,
                timeout_seconds,
                cancellation,
                interruption=lambda: self._connection_error(client),
            )
        except BaseException as exc:
            if isinstance(exc, MCPClientError):
                exc.details = exc.details | {"outcome_unknown": dispatched}
                if exc.code == "MCP_CONNECTION_LOST" and client is not None:
                    self._invalidate(client)
            raise

    def _connection_error(self, client: Client | None) -> MCPClientError | None:
        if self._closed or (
            client is not None and (self._client is not client or self._stop.is_set())
        ):
            return MCPClientError(
                "MCP_CONNECTION_LOST",
                "MCP connection closed during the request.",
                retryable=True,
            )
        return None

    async def _connect_locked(self) -> None:
        if self._closed:
            raise MCPClientError("MCP_NOT_STARTED", "Open the MCP client before accepting runs.")
        if self.is_ready:
            return
        if self._owner is not None and not self._stop.is_set() and not self._owner.done():
            # Another caller timed out while connection startup continued under its own budget.
            await self._ready.wait()
        else:
            if self._owner is not None:
                await asyncio.shield(self._owner)
            if self._closed:
                raise MCPClientError("MCP_NOT_STARTED", "MCP client is closing.")
            self._ready = asyncio.Event()
            self._stop = asyncio.Event()
            self._error = None
            self._owner = asyncio.create_task(self._serve_connection(), name="mcp-session")
            await self._ready.wait()
        if not self.is_ready:
            raise self._error or MCPClientError(
                "MCP_CONNECTION_LOST", "MCP connection failed to become ready.", retryable=True
            )

    async def _serve_connection(self) -> None:
        try:
            with anyio.move_on_after(self._startup_timeout) as scope:
                self._lifecycle_scope = scope
                async with Client(
                    cast(Any, self._target),
                    read_timeout_seconds=self._startup_timeout,
                    input_required_max_rounds=0,
                ) as client:
                    catalog = await self._fetch_catalog(client)
                    self._catalog = catalog
                    self._client = client
                    self._version += 1
                    scope.deadline = float("inf")
                    self._ready.set()
                    await self._stop.wait()
            if scope.cancelled_caught and not self._closed and not self._stop.is_set():
                self._error = MCPClientError(
                    "MCP_TIMEOUT", "MCP startup timed out.", retryable=True
                )
        except Exception as exc:
            error = classify_client_error(exc)
            self._error = MCPClientError(
                error.code, error.message, retryable=error.retryable, details=error.details
            )
        finally:
            self._client = None
            self._catalog = ()
            self._lifecycle_scope = None
            self._version += 1
            self._ready.set()

    def _invalidate(self, client: Client) -> None:
        if client is self._client and not self._stop.is_set():
            self._version += 1
            self._stop.set()

    async def _fetch_catalog(self, client: Client) -> tuple[DiscoveredTool, ...]:
        discovered: list[DiscoveredTool] = []
        cursor: str | None = None
        while True:
            listed = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for tool in listed.tools:
                schema = as_jsonable(tool.input_schema)
                if not isinstance(schema, dict):
                    raise MCPClientError(
                        "MCP_MALFORMED_TOOL_SCHEMA", "Tool schema is not an object."
                    )
                try:
                    Draft202012Validator.check_schema(schema)
                except SchemaError as exc:
                    raise MCPClientError("MCP_MALFORMED_TOOL_SCHEMA", exc.message) from exc
                annotations = tool.annotations
                retry_safe = bool(
                    annotations
                    and (
                        annotations.read_only_hint is True
                        or (
                            annotations.idempotent_hint is True
                            and annotations.destructive_hint is not True
                        )
                    )
                )
                discovered.append(
                    DiscoveredTool(
                        ToolDefinition(tool.name, tool.description or "", schema), retry_safe
                    )
                )
            if listed.next_cursor is None:
                return tuple(discovered)
            cursor = listed.next_cursor


def classify_client_error(
    exc: Exception,
    *,
    default_code: str = "MCP_CLIENT_ERROR",
) -> RuntimeError:
    if isinstance(exc, MCPClientError):
        return RuntimeError(exc.code, str(exc), exc.retryable, exc.details)
    if isinstance(exc, OperationCancelled):
        return RuntimeError("TOOL_CANCELLED", str(exc))

    if isinstance(exc, ExceptionGroup):
        errors = [
            classify_client_error(nested, default_code=default_code) for nested in exc.exceptions
        ]
        selected = next((error for error in errors if not error.retryable), errors[0])
        return RuntimeError(
            selected.code,
            selected.message,
            selected.retryable,
            selected.details | {"causes": [as_jsonable(error) for error in errors]},
        )
    if isinstance(exc, PermissionError):
        return RuntimeError("MCP_PERMISSION_DENIED", str(exc))
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return RuntimeError("MCP_CONFIGURATION_ERROR", str(exc))
    if isinstance(exc, TimeoutError) or (isinstance(exc, MCPError) and exc.code == REQUEST_TIMEOUT):
        return RuntimeError(
            "MCP_TIMEOUT",
            str(exc) or "The MCP request timed out.",
            retryable=True,
        )
    connection_types = (
        ConnectionError,
        BrokenResourceError,
        ClosedResourceError,
        EndOfStream,
    )
    transient_errno = {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }
    if (
        isinstance(exc, connection_types)
        or (isinstance(exc, OSError) and exc.errno in transient_errno)
        or (isinstance(exc, MCPError) and exc.code == CONNECTION_CLOSED)
    ):
        return RuntimeError(
            "MCP_CONNECTION_LOST",
            str(exc) or "The MCP connection was lost.",
            retryable=True,
        )
    if isinstance(exc, MCPError):
        return RuntimeError(
            "MCP_PROTOCOL_ERROR",
            str(exc) or "The MCP server returned a protocol error.",
        )
    return RuntimeError(default_code, str(exc) or "The MCP client failed.")


def _normalize_result(result: CallToolResult) -> MCPToolResponse:
    if result.structured_content is not None:
        content = as_jsonable(result.structured_content)
    else:
        content = [
            as_jsonable(block.model_dump(mode="json", by_alias=True, exclude_none=True))
            for block in result.content
        ]
    return MCPToolResponse(
        model_content=content,
        is_error=result.is_error,
        error_message=_error_message(content) if result.is_error else None,
    )


def _error_message(content: JSONValue) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return cast(str, item["text"])
    return "The MCP tool returned an error."


async def _bounded[T](
    operation: Callable[[], Coroutine[Any, Any, T]],
    timeout_seconds: float,
    cancellation: CancellationToken | None = None,
    *,
    interruption: Callable[[], Exception | None] | None = None,
) -> T:
    try:
        return await cancellable(
            operation, timeout_seconds, cancellation, interruption=interruption
        )
    except MCPClientError:
        raise
    except Exception as exc:
        error = classify_client_error(exc)
        raise MCPClientError(
            error.code, error.message, retryable=error.retryable, details=error.details
        ) from exc
