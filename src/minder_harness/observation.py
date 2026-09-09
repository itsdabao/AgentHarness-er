"""Client-only cursor handling; no execution or persistence ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .rpc_client import RPCClient

ACTIVE = {"queued", "running", "cancel_requested"}


def accept_page(page: dict[str, Any], cursor: int) -> tuple[list[dict[str, Any]], int]:
    events = sorted(page["events"], key=lambda e: e["sequence"])
    accepted: list[dict[str, Any]] = []
    for event in events:
        sequence = event["sequence"]
        if not isinstance(sequence, int) or sequence < 1:
            raise ValueError("Invalid event sequence")
        if sequence > cursor:
            accepted.append(event)
            cursor = sequence
    if page["has_more"] and not accepted:
        raise ValueError("Event page made no progress")
    return accepted, cursor


async def watch_run(
    client: RPCClient,
    run_id: str,
    cursors: dict[str, int],
    receive: Callable[[str, dict[str, Any]], None],
    interval: float = 0.5,
) -> dict[str, Any]:
    """Confirm terminal state before draining the last committed event pages."""
    while True:
        state = await client.call("get_run", run_id=run_id)
        receive("snapshot", state)
        while True:
            cursor = cursors.get(run_id, 0)
            page = await client.call("list_run_events", run_id=run_id, after_sequence=cursor)
            events, next_cursor = accept_page(page, cursor)
            for event in events:
                receive("event", event)
            cursors[run_id] = next_cursor
            if not page["has_more"]:
                break
            await asyncio.sleep(0)
        if state["status"] not in ACTIVE:
            receive("terminal", state)
            return state
        await asyncio.sleep(interval)
