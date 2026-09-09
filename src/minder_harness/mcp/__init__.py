"""MCP infrastructure boundary for the agent runtime."""

from .client import SDKMCPClient
from .contracts import DiscoveredTool, MCPClientBackend, MCPClientError, MCPToolResponse
from .executor import MCPToolExecutor

__all__ = [
    "DiscoveredTool",
    "MCPClientBackend",
    "MCPClientError",
    "MCPToolExecutor",
    "MCPToolResponse",
    "SDKMCPClient",
]
