from __future__ import annotations

import ctypes
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS, REQUEST_TIMEOUT

from minder_harness.mcp import MCPClientError, SDKMCPClient
from minder_harness.mcp.client import classify_client_error


@pytest.mark.parametrize(
    ("exception", "code", "retryable"),
    [
        (PermissionError("denied"), "MCP_PERMISSION_DENIED", False),
        (FileNotFoundError("missing executable"), "MCP_CONFIGURATION_ERROR", False),
        (OSError(errno.EINVAL, "bad config"), "MCP_CLIENT_ERROR", False),
        (OSError(errno.ENETUNREACH, "network down"), "MCP_CONNECTION_LOST", True),
        (ConnectionResetError("reset"), "MCP_CONNECTION_LOST", True),
        (TimeoutError("slow"), "MCP_TIMEOUT", True),
        (MCPError(REQUEST_TIMEOUT, "slow"), "MCP_TIMEOUT", True),
        (MCPError(CONNECTION_CLOSED, "closed"), "MCP_CONNECTION_LOST", True),
        (MCPError(INVALID_PARAMS, "bad args"), "MCP_PROTOCOL_ERROR", False),
        (ValueError("unknown failure"), "MCP_CLIENT_ERROR", False),
        (
            ExceptionGroup("mixed", [TimeoutError(), PermissionError("denied")]),
            "MCP_PERMISSION_DENIED",
            False,
        ),
        (
            ExceptionGroup("mixed", [TimeoutError(), ValueError("unknown")]),
            "MCP_CLIENT_ERROR",
            False,
        ),
        (
            ExceptionGroup("transient", [TimeoutError(), ConnectionResetError()]),
            "MCP_TIMEOUT",
            True,
        ),
    ],
)
def test_error_classification(exception: Exception, code: str, retryable: bool) -> None:
    result = classify_client_error(exception)
    assert result.code == code
    assert result.retryable is retryable


@pytest.mark.anyio
async def test_missing_server_reports_known_pre_dispatch_failure(tmp_path: Path) -> None:
    from mcp import StdioServerParameters

    client = SDKMCPClient(StdioServerParameters(command=str(tmp_path / "missing-server")))
    with pytest.raises(MCPClientError) as caught:
        async with client:
            pytest.fail("Missing executable must fail at startup")
    assert caught.value.code == "MCP_CONFIGURATION_ERROR"
    assert caught.value.retryable is False
    assert not client.is_ready


def _process_exited(pid: int) -> bool:
    if sys.platform == "win32":
        # Read-only process query; never signal or terminate a PID that may have been reused.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID no longer exists
        try:
            return bool(kernel.WaitForSingleObject(handle, 0) == 0)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize(
    ("mode", "expected_code", "calls"),
    [
        ("slow", "MCP_TIMEOUT", 1),
        ("stubborn", "MCP_TIMEOUT", 1),
        ("cancel", "TOOL_CANCELLED", 1),
        ("drop", "MCP_CONNECTION_LOST", 2),
        ("retry_success", None, 2),
        ("business_error", "MCP_TOOL_ERROR", 1),
        ("input_required", "MCP_CLIENT_ERROR", 1),
        ("discovery_timeout", "MCP_TIMEOUT", 0),
        ("reuse", None, 3),
    ],
)
def test_real_stdio_failures_are_bounded_and_traceable(
    tmp_path: Path,
    mode: str,
    expected_code: str | None,
    calls: int,
) -> None:
    log = tmp_path / "invocations.jsonl"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "fixtures" / "mcp_probe.py"), mode, str(log)],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(entry["event"] == "tool_called" for entry in entries) == calls
    assert all(_process_exited(entry["pid"]) for entry in entries)
    # Includes SDK's bounded process shutdown grace, not only request latency.
    assert report["elapsed"] < (12.0 if mode in {"retry_success", "drop"} else 8.0)
    if mode == "discovery_timeout":
        assert report["code"] == expected_code
        return
    result = report["result"]
    assert result["metadata"]["attempt_count"] == (1 if mode == "reuse" else calls)
    if mode in {"reuse", "retry_success", "drop"}:
        expected_connections = 1 if mode == "reuse" else 2
        assert sum(e["event"] == "server_started" for e in entries) == expected_connections
        assert sum(e["event"] == "tools_list" for e in entries) == expected_connections
    if expected_code is None:
        assert result["status"] == "succeeded"
        assert result["model_content"] == {"ok": True}
        assert [event["type"] for event in report["events"]] == (
            [] if mode == "reuse" else ["tool_execution_failed", "tool_execution_started"]
        )
    else:
        assert result["error"]["code"] == expected_code
        if mode != "business_error":
            assert result["error"]["details"]["outcome_unknown"] is True
