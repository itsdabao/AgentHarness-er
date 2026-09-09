from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..core.cancellation import CancellationToken
from ..core.models import JSONValue, ToolDefinition


@dataclass(frozen=True)
class DiscoveredTool:
    definition: ToolDefinition
    retry_safe: bool = False


@dataclass(frozen=True)
class MCPToolResponse:
    model_content: JSONValue
    is_error: bool = False
    error_message: str | None = None


class MCPClientBackend(Protocol):
    @property
    def catalog_version(self) -> int: ...

    async def list_tools(
        self,
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        refresh: bool = False,
    ) -> Sequence[DiscoveredTool]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JSONValue],
        timeout_seconds: float,
        cancellation: CancellationToken | None = None,
        *,
        expected_catalog_version: int | None = None,
    ) -> MCPToolResponse: ...


class MCPClientError(Exception):
    """Normalized failure; details.outcome_unknown tracks uncertain dispatch separately."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = {} if details is None else details
