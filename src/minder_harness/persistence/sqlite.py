from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import aiosqlite
import anyio

from ..core.cancellation import OperationCancelled
from ..core.codec import message_from, run_from, state_from
from ..core.context import Fact, TaskState, json_size
from ..core.models import (
    Event,
    JSONValue,
    Message,
    Run,
    RunStatus,
    Session,
    ToolCall,
    ToolResult,
    as_jsonable,
    utc_now_iso,
)
from ..core.ports import ReplayBlocked, RunConflict, StorageError
from .ownership import DatabaseOwnership
from .schema import MIGRATIONS

ACTIVE = {"queued", "running", "cancel_requested"}
TERMINAL_EVENTS: dict[str, RunStatus] = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_cancelled": "cancelled",
    "run_limit_exceeded": "limit_exceeded",
}


def encode(value: object) -> str:
    return json.dumps(as_jsonable(value), ensure_ascii=False, sort_keys=True)


class SQLiteExecutionStore:
    """One connection and serialized short transactions. Never lock across external I/O."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._owner = DatabaseOwnership(self.path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._broken = False

    async def __aenter__(self) -> SQLiteExecutionStore:
        if self._db is not None:
            raise ValueError("Store is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owner.acquire()
        try:
            self._db = await aiosqlite.connect(self.path, isolation_level=None)
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=FULL")
            async with self._transaction() as db:
                row = await self._one(db, "PRAGMA user_version")
                version = int(row[0])
                if version > len(MIGRATIONS):
                    raise ValueError("Unsupported future database schema")
                for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
                    for statement in statements:
                        await db.execute(statement)
                    await db.execute(f"PRAGMA user_version={number}")
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if self._db is not None:
                    await self._db.close()
                    self._db = None
                self._owner.release()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            if self._db is None or self._broken:
                raise StorageError("Store is closed or quarantined after a recording failure")
            db = self._db
            with anyio.CancelScope(shield=True):
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    yield db
                    await db.commit()
                except BaseException as exc:
                    domain_error = isinstance(
                        exc, (KeyError, ValueError, ReplayBlocked, OperationCancelled)
                    )
                    if not domain_error:
                        self._broken = True
                    await db.rollback()
                    if domain_error or not isinstance(exc, Exception):
                        raise
                    raise StorageError("Database operation failed; new work is disabled") from exc

    @staticmethod
    async def _one(db: aiosqlite.Connection, sql: str, args: tuple[Any, ...] = ()) -> Any:
        async with db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError("Record not found")
        return row

    async def _load_run(self, db: aiosqlite.Connection, run_id: str) -> Run:
        row = await self._one(db, "SELECT body FROM runs WHERE id=?", (run_id,))
        return run_from(json.loads(row[0]))

    async def _save_run(self, db: aiosqlite.Connection, run: Run) -> None:
        snapshot = run.to_dict()
        snapshot.pop("events", None)
        await db.execute(
            "UPDATE runs SET status=?, body=? WHERE id=?",
            (run.status, encode(snapshot), run.run_id),
        )

    async def _event(
        self,
        db: aiosqlite.Connection,
        run: Run,
        kind: str,
        payload: dict[str, JSONValue],
    ) -> Event:
        row = await self._one(
            db,
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?",
            (run.run_id,),
        )
        event = Event(
            f"evt_{uuid4().hex}", run.session_id, run.run_id, row[0], kind, utc_now_iso(), payload
        )
        await db.execute(
            "INSERT INTO events VALUES(?,?,?,?)",
            (run.run_id, event.sequence, event.event_id, encode(event)),
        )
        return event

    async def _message(
        self,
        db: aiosqlite.Connection,
        session_id: str,
        run_id: str | None,
        message: Message,
        accepted: bool = True,
    ) -> None:
        await db.execute(
            "INSERT INTO messages(id,session_id,run_id,accepted,body) VALUES(?,?,?,?,?)",
            (message.message_id, session_id, run_id, int(accepted), encode(message)),
        )
        row = await self._one(db, "SELECT body FROM sessions WHERE id=?", (session_id,))
        session = json.loads(row[0])
        session["updated_at"] = utc_now_iso()
        await db.execute("UPDATE sessions SET body=? WHERE id=?", (encode(session), session_id))

    async def create_session(self, session: Session) -> Session:
        async with self._transaction() as db:
            body = as_jsonable(replace(session, messages=[]))
            await db.execute("INSERT INTO sessions VALUES(?,?)", (session.session_id, encode(body)))
            await db.execute(
                "INSERT INTO task_states VALUES(?,?)", (session.session_id, encode(TaskState()))
            )
            for message in session.messages:
                await self._message(db, session.session_id, None, message)
        return session

    async def get_session(self, session_id: str) -> Session:
        async with self._transaction() as db:
            row = await self._one(db, "SELECT body FROM sessions WHERE id=?", (session_id,))
            data = json.loads(row[0])
            async with db.execute(
                "SELECT m.body,r.status FROM messages m LEFT JOIN runs r ON r.id=m.run_id "
                "WHERE m.session_id=? AND m.accepted=1 ORDER BY m.ordinal",
                (session_id,),
            ) as cursor:
                messages = []
                for body, status in await cursor.fetchall():
                    message = message_from(json.loads(body))
                    # Audit body is immutable; only committed final answers enter continuation.
                    if (
                        message.role == "assistant"
                        and not message.tool_calls
                        and status is not None
                        and status != "completed"
                    ):
                        continue
                    messages.append(message)
            return Session(**(data | {"messages": messages}))

    async def list_sessions(
        self,
        before: int | None = None,
        limit: int = 20,
    ) -> dict[str, JSONValue]:
        if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= 2**53 - 1):
            raise ValueError("Invalid session page")
        # Stable insertion-order cursor; updating a running session does not reorder pages.
        query = """
            SELECT s.rowid, s.id,
                   json_extract(s.body, '$.created_at'),
                   json_extract(s.body, '$.updated_at'),
                   (SELECT substr(json_extract(m.body, '$.content'), 1, 160)
                    FROM messages m WHERE m.session_id=s.id AND m.accepted=1
                    AND json_extract(m.body, '$.role')='user'
                    ORDER BY m.ordinal LIMIT 1),
                   r.id, r.status
            FROM sessions s
            LEFT JOIN runs r ON r.rowid=(
                SELECT rowid FROM runs WHERE session_id=s.id ORDER BY rowid DESC LIMIT 1
            )
            WHERE s.rowid < ?
            ORDER BY s.rowid DESC LIMIT ?
        """
        async with self._transaction() as db:
            async with db.execute(query, (before or 2**53 - 1, limit + 1)) as cursor:
                rows = list(await cursor.fetchall())
        page = rows[:limit]
        items: list[JSONValue] = [
            {
                "session_id": row[1],
                "title": " ".join(str(row[4] or "").split())[:80] or "Phiên chưa có nhiệm vụ",
                "created_at": row[2],
                "updated_at": row[3],
                "latest_run_id": row[5],
                "latest_run_status": row[6],
            }
            for row in page
        ]
        return {
            "sessions": items,
            "has_more": len(rows) > limit,
            "next_before": page[-1][0] if page else before,
        }

    async def get_task_state(self, session_id: str) -> TaskState:
        async with self._transaction() as db:
            row = await self._one(
                db, "SELECT body FROM task_states WHERE session_id=?", (session_id,)
            )
            return state_from(json.loads(row[0]))

    async def create_run(self, run: Run, message: Message, state: TaskState) -> None:
        if state.schema_version != 1 or json_size(state) > 6_000:
            raise ValueError("Invalid or oversized task state")
        async with self._transaction() as db:
            row = await self._one(
                db,
                "SELECT COUNT(*) FROM runs WHERE session_id=? AND status IN "
                "('queued','running','cancel_requested')",
                (run.session_id,),
            )
            if row[0]:
                raise RunConflict("A run is already active for this session")
            await db.execute(
                "INSERT INTO runs VALUES(?,?,?,?)",
                (run.run_id, run.session_id, run.status, encode(run)),
            )
            await self._message(db, run.session_id, run.run_id, message)
            await db.execute(
                "UPDATE task_states SET body=? WHERE session_id=?", (encode(state), run.session_id)
            )
            await self._event(
                db,
                run,
                "run_queued",
                {
                    "message_id": message.message_id,
                    "task_state": as_jsonable(state),
                },
            )

    async def get_run(self, run_id: str) -> Run:
        async with self._transaction() as db:
            return await self._load_run(db, run_id)

    async def record_event(
        self,
        run: Run,
        kind: str,
        payload: dict[str, JSONValue] | None,
    ) -> Event:
        data = {} if payload is None else dict(payload)
        async with self._transaction() as db:
            current = await self._load_run(db, run.run_id)
            if kind == "run_started" and current.status == "queued":
                current.status = "running"
                current.started_at = run.started_at
            elif kind == "run_cancel_requested" and current.status in ACTIVE:
                current.status = "cancel_requested"
                current.cancel_requested_at = current.cancel_requested_at or utc_now_iso()
            elif kind in TERMINAL_EVENTS and current.status in ACTIVE:
                target = (
                    "cancelled" if current.status == "cancel_requested" else TERMINAL_EVENTS[kind]
                )
                current = replace(
                    run,
                    status=target,
                    events=[],
                    cancel_requested_at=current.cancel_requested_at,
                    output=None if target == "cancelled" else run.output,
                    finished_at=utc_now_iso(),
                )
                kind = f"run_{target}"
                if target == "cancelled":
                    data = {}
            elif current.status not in ACTIVE:
                kind = "late_result_recorded"
                data["_accepted"] = False
            if kind == "model_request_started" and data.get("record_type") != "provider_attempt":
                current.usage.model_steps += 1
            if kind == "tool_call_requested":
                current.usage.tool_calls += 1
            message_data = data.get("_message")
            if isinstance(message_data, dict):
                accepted = (
                    data.get("_accepted", True) is not False
                    and current.status != "cancel_requested"
                )
                # Messages arriving after a terminal transition are diagnostic only.
                if current.status not in ACTIVE:
                    accepted = False
                message = message_from(message_data)
                await self._message(db, current.session_id, current.run_id, message, accepted)
                if accepted and kind == "tool_execution_completed":
                    row = await self._one(
                        db, "SELECT body FROM task_states WHERE session_id=?", (current.session_id,)
                    )
                    state = state_from(json.loads(row[0]))
                    observation = Fact(message.content, message.message_id, utc_now_iso())
                    state = replace(
                        state,
                        revision=state.revision + 1,
                        observations=(state.observations + (observation,))[-8:],
                    )
                    while json_size(state) > 6_000 and state.observations:
                        state = replace(state, observations=state.observations[1:])
                    await db.execute(
                        "UPDATE task_states SET body=? WHERE session_id=?",
                        (encode(state), current.session_id),
                    )
            await self._save_run(db, current)
            return await self._event(db, current, kind, data)

    async def cancel_run(self, run_id: str) -> Run:
        async with self._transaction() as db:
            run = await self._load_run(db, run_id)
            if run.status not in ACTIVE or run.status == "cancel_requested":
                return run
            run.cancel_requested_at = utc_now_iso()
            if run.status == "queued":
                run.status = "cancelled"
                run.finished_at = utc_now_iso()
                await self._event(db, run, "run_cancel_requested", {})
                await self._event(db, run, "run_cancelled", {})
            else:
                run.status = "cancel_requested"
                await self._event(db, run, "run_cancel_requested", {})
            await self._save_run(db, run)
            return run

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, JSONValue]:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("Invalid event cursor or page size")
        async with self._transaction() as db:
            await self._load_run(db, run_id)
            async with db.execute(
                "SELECT body FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (run_id, after_sequence, limit + 1),
            ) as cursor:
                records = [json.loads(row[0]) for row in await cursor.fetchall()]
            page = records[:limit]
            return {
                "events": page,
                "has_more": len(records) > limit,
                "next_after_sequence": page[-1]["sequence"] if page else after_sequence,
            }

    async def recover(self) -> None:
        async with self._transaction() as db:
            async with db.execute(
                "SELECT body FROM runs WHERE status IN ('queued','running','cancel_requested')"
            ) as cursor:
                runs = [run_from(json.loads(row[0])) for row in await cursor.fetchall()]
            for run in runs:
                previous = run.status
                run.status = "cancelled" if previous == "cancel_requested" else "interrupted"
                run.finished_at = utc_now_iso()
                run.output = None
                await self._save_run(db, run)
                await self._event(
                    db, run, f"run_{run.status}", {"recovered_from": previous, "replayed": False}
                )

    async def begin_attempt(
        self,
        call: ToolCall,
        scope: str,
        attempt: int,
        retry_safe: bool,
    ) -> str:
        if not call.run_id or not call.execution_id or not scope:
            raise ValueError("Durable attempts require run/execution identity and server scope")
        key = encode({"name": call.name, "arguments": call.arguments})
        async with self._transaction() as db:
            run = await self._load_run(db, call.run_id)
            if run.status != "running":
                raise OperationCancelled("Run is no longer running")
            row = await self._one(
                db,
                "SELECT COUNT(*) FROM attempts WHERE scope=? AND call_key=? "
                "AND retry_safe=0 AND outcome_unknown=1",
                (scope, key),
            )
            if row[0]:
                raise ReplayBlocked("A matching unsafe operation has an unresolved outcome")
            attempt_id = f"attempt_{uuid4().hex}"
            await db.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    attempt_id,
                    call.execution_id,
                    call.run_id,
                    attempt,
                    scope,
                    key,
                    int(retry_safe),
                    "pending",
                    1,
                    encode(call),
                ),
            )
            await self._event(
                db,
                run,
                "tool_execution_started",
                {
                    "record_type": "attempt_intent",
                    "attempt_id": attempt_id,
                    "execution_id": call.execution_id,
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                    "attempt": attempt,
                    "retry_safe": retry_safe,
                    "scope": scope,
                },
            )
            return attempt_id

    async def end_attempt(
        self,
        attempt_id: str,
        result: ToolResult,
        outcome_unknown: bool,
    ) -> None:
        async with self._transaction() as db:
            row = await self._one(db, "SELECT run_id,state FROM attempts WHERE id=?", (attempt_id,))
            if row[1] != "pending":
                raise ValueError("Attempt already finalized")
            await db.execute(
                "UPDATE attempts SET state=?,outcome_unknown=?,result_json=? WHERE id=?",
                (result.status, int(outcome_unknown), encode(result), attempt_id),
            )
            run = await self._load_run(db, row[0])
            await self._event(
                db,
                run,
                "tool_execution_completed"
                if result.status == "succeeded"
                else "tool_execution_failed",
                result.to_dict()
                | {
                    "record_type": "attempt_result",
                    "attempt_id": attempt_id,
                    "outcome_unknown": outcome_unknown,
                },
            )

    async def execution_outcomes(self, session_id: str) -> dict[str, str]:
        async with self._transaction() as db:
            async with db.execute(
                "SELECT a.execution_id,a.state,a.outcome_unknown,a.result_json FROM attempts a "
                "JOIN runs r ON r.id=a.run_id WHERE r.session_id=? ORDER BY a.attempt",
                (session_id,),
            ) as cursor:
                outcomes: dict[str, str] = {}
                for execution_id, state, unknown, result_json in await cursor.fetchall():
                    result = {} if result_json is None else json.loads(result_json)
                    code = (result.get("error") or {}).get("code")
                    if unknown:
                        outcome = "outcome_unknown"
                    elif state == "reconciled":
                        outcome = "reconciled_by_operator"
                    elif state == "succeeded":
                        outcome = "result_recorded_not_accepted"
                    elif code == "TOOL_CANCELLED":
                        outcome = "cancelled"
                    elif code in {"TOOL_TIMEOUT", "MCP_CONFIGURATION_ERROR", "MCP_CATALOG_CHANGED"}:
                        outcome = "not_executed"
                    else:
                        outcome = "failed"
                    outcomes[execution_id] = outcome
                return outcomes

    async def resolve_attempt(self, attempt_id: str, note: str) -> None:
        """Explicit operator reconciliation only. Never exposed as an agent tool."""
        if not note.strip():
            raise ValueError("Reconciliation requires evidence or an operator note")
        async with self._transaction() as db:
            row = await self._one(db, "SELECT run_id FROM attempts WHERE id=?", (attempt_id,))
            run = await self._load_run(db, row[0])
            if run.status in ACTIVE:
                raise ValueError("Cannot reconcile an attempt while its run is active")
            await db.execute(
                "UPDATE attempts SET state='reconciled',outcome_unknown=0 WHERE id=?", (attempt_id,)
            )
            await self._event(
                db, run, "tool_outcome_reconciled", {"attempt_id": attempt_id, "note": note}
            )
