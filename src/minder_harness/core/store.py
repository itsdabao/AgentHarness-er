"""One persistence boundary used by the application, never SQL inside AgentLoop."""

from __future__ import annotations

from typing import Protocol

from .context import TaskState
from .models import Event, JSONValue, Message, Run, Session
from .ports import AttemptRecorder


class ExecutionStore(AttemptRecorder, Protocol):
    async def create_session(self, session: Session) -> Session: ...
    async def get_session(self, session_id: str) -> Session: ...
    async def list_sessions(
        self,
        before: int | None = None,
        limit: int = 20,
    ) -> dict[str, JSONValue]: ...
    async def get_task_state(self, session_id: str) -> TaskState: ...
    async def create_run(self, run: Run, message: Message, state: TaskState) -> None: ...
    async def get_run(self, run_id: str) -> Run: ...
    async def record_event(
        self,
        run: Run,
        kind: str,
        payload: dict[str, JSONValue] | None,
    ) -> Event: ...
    async def cancel_run(self, run_id: str) -> Run: ...
    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, JSONValue]: ...
    async def recover(self) -> None: ...
    async def execution_outcomes(self, session_id: str) -> dict[str, str]: ...
