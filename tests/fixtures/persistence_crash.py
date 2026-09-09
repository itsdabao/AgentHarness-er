"""Hard process-exit fixture; all files are under a parent pytest temporary directory."""

import os
import sys
from pathlib import Path

import anyio

from minder_harness.core.context import Fact, TaskState
from minder_harness.core.models import (
    ExecutionLimits,
    Message,
    Run,
    Session,
    ToolCall,
    ToolResult,
    as_jsonable,
)
from minder_harness.persistence import SQLiteExecutionStore


async def main() -> None:
    path, mode = sys.argv[1:]
    async with SQLiteExecutionStore(path) as store:
        await store.create_session(Session("session"))
        run = Run("run", "session", "queued", ExecutionLimits())
        user = Message("user", "update")
        await store.create_run(run, user, TaskState(objective=Fact("update", user.message_id)))
        await store.record_event(run, "run_started", {})
        call = ToolCall("provider-id", "update", {"mode": "idle"}, "run", "execution")
        await store.record_event(
            run,
            "model_response_received",
            {
                "_message": as_jsonable(Message("assistant", None, tool_calls=(call,))),
            },
        )
        if mode == "before_intent":
            os._exit(17)
        attempt = await store.begin_attempt(call, "server/operator", 1, False)
        if mode == "after_intent":
            os._exit(17)
        # Stand-in remote side effect to expose the crash window, not a runtime tool.
        Path(path).with_suffix(".effect").write_text("performed", encoding="utf-8")
        if mode == "after_remote":
            os._exit(17)
        await store.end_attempt(attempt, ToolResult.success(call, {"ok": True}), False)
        os._exit(17)


anyio.run(main)
