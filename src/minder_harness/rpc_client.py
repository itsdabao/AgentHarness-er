"""Small reusable HTTP client. Submission is never automatically retried."""

from __future__ import annotations

from itertools import count
from typing import Any

import httpx


class RPCError(ValueError):
    """A confirmed RPC rejection, not an ambiguous transport failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"RPC operation failed: {code}")


class RPCClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self._ids = count(1)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        request_id = next(self._ids)
        response = await self.http.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("id") != request_id or body.get("jsonrpc") != "2.0":
            raise ValueError("Invalid RPC response identity.")
        if "error" in body:
            raise RPCError(str(body["error"]["code"]))
        result = body["result"]
        if not result["ok"]:
            raise RPCError(result["error"]["code"])
        data: dict[str, Any] = result["data"]
        return data
