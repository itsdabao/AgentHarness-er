"""Transport-neutral application lifecycle around the existing AgentHarness."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from types import TracebackType
from uuid import uuid4

from .core.cancellation import CancellationToken
from .core.context import Fact, continue_transcript, json_size
from .core.harness import AgentHarness
from .core.models import Event, ExecutionLimits, JSONValue, Message, Run, Session, ToolDefinition
from .core.ports import StorageError
from .core.store import ExecutionStore


class AgentService:
    def __init__(
        self,
        harness: AgentHarness,
        store: ExecutionStore,
        *,
        tools: Sequence[ToolDefinition] = (),
        context_limit: int = 24_000,
        shutdown_timeout: float = 5,
    ) -> None:
        if shutdown_timeout <= 0 or context_limit < 1:
            raise ValueError("Timeout and context limit must be positive")
        self.harness = harness
        self.store = store
        self.tools = tuple(tools)
        self.context_limit = context_limit
        self.shutdown_timeout = shutdown_timeout
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._admission = asyncio.Lock()
        self._accepting = False
        self._fault: BaseException | None = None

    async def __aenter__(self) -> AgentService:
        if self._accepting or self._tasks:
            raise ValueError("Service already started")
        await self.store.recover()
        self._accepting = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.shutdown()

    def _healthy(self) -> None:
        if self._fault is not None:
            raise StorageError(
                "Runtime stopped after a lifecycle/recording failure"
            ) from self._fault

    @property
    def is_ready(self) -> bool:
        return self._accepting and self._fault is None

    async def create_session(self, metadata: dict[str, JSONValue] | None = None) -> Session:
        self._healthy()
        if not self._accepting:
            raise ValueError("Runtime is not accepting new work")
        return await self.store.create_session(
            Session(f"ses_{uuid4().hex}", metadata={} if metadata is None else metadata)
        )

    async def list_sessions(
        self,
        before: int | None = None,
        limit: int = 20,
    ) -> dict[str, JSONValue]:
        self._healthy()
        return await self.store.list_sessions(before, limit)

    async def submit_task(
        self,
        session_id: str,
        content: JSONValue,
        *,
        constraints: Sequence[str] | None = None,
        open_questions: Sequence[str] | None = None,
        limits: ExecutionLimits | None = None,
    ) -> Run:
        async with self._admission:
            self._healthy()
            if not self._accepting:
                raise ValueError("Runtime is not accepting new work")
            await self.store.get_session(session_id)
            previous = await self.store.get_task_state(session_id)
            message = Message("user", content)
            state = replace(
                previous,
                revision=previous.revision + 1,
                objective=Fact(content, message.message_id),
                constraints=previous.constraints
                if constraints is None
                else tuple(Fact(value, message.message_id) for value in constraints),
                open_questions=previous.open_questions
                if open_questions is None
                else tuple(Fact(value, message.message_id) for value in open_questions),
            )
            while json_size(state) > 6_000 and state.observations:
                state = replace(state, observations=state.observations[1:])
            run = Run(
                f"run_{uuid4().hex}",
                session_id,
                "queued",
                ExecutionLimits() if limits is None else limits,
            )
            await self.store.create_run(run, message, state)
            token = CancellationToken()
            self._tokens[run.run_id] = token
            task = asyncio.create_task(self._execute(run, token), name=f"agent:{run.run_id}")
            self._tasks[run.run_id] = task
            task.add_done_callback(lambda done: self._observe(run.run_id, done))
            return replace(run)

    def _observe(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._tokens.pop(run_id, None)
        if not task.cancelled():
            error = task.exception()  # Always retrieve failures, even if no caller waits.
            if error is not None:
                self._fault = error
                self._accepting = False
                for token in self._tokens.values():
                    token.cancel()

    async def _execute(self, run: Run, token: CancellationToken) -> None:
        if (await self.store.get_run(run.run_id)).status != "queued":
            return
        session = await self.store.get_session(run.session_id)
        state = await self.store.get_task_state(run.session_id)
        outcomes = await self.store.execution_outcomes(run.session_id)
        session.messages = list(continue_transcript(session.messages, outcomes))

        async def record(kind: str, payload: dict[str, JSONValue] | None) -> Event:
            return await self.store.record_event(run, kind, payload)

        try:
            await self.harness.run(
                session,
                None,
                cancellation=token,
                tools=self.tools,
                existing_run=run,
                event_sink=record,
                recorder=self.store,
                task_state=state,
                context_limit=self.context_limit,
            )
        except StorageError:
            token.cancel()
            raise
        except asyncio.CancelledError:
            token.cancel()
            # Best effort under forced local shutdown; interrupted recovery remains the fallback.
            await self.store.cancel_run(run.run_id)
            run.status = "cancelled"
            await self.store.record_event(run, "run_cancelled", {})
            raise

    async def get_run(self, run_id: str) -> Run:
        self._healthy()
        return await self.store.get_run(run_id)

    async def get_session(self, session_id: str) -> Session:
        self._healthy()
        return await self.store.get_session(session_id)

    async def wait_run(self, run_id: str) -> Run:
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        return await self.get_run(run_id)

    async def cancel_run(self, run_id: str) -> Run:
        try:
            return await self.store.cancel_run(run_id)
        finally:
            token = self._tokens.get(run_id)
            if token is not None:
                token.cancel()

    async def list_run_events(
        self,
        run_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, JSONValue]:
        return await self.store.list_run_events(run_id, after_sequence, limit)

    async def shutdown(self) -> None:
        async with self._admission:
            self._accepting = False
        tasks = list(self._tasks.values())
        for run_id, token in list(self._tokens.items()):
            token.cancel()
            try:
                await self.store.cancel_run(run_id)
            except StorageError as exc:
                self._fault = self._fault or exc
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_timeout)
            for task in pending:
                task.cancel()
            if pending:
                _, still_pending = await asyncio.wait(pending, timeout=self.shutdown_timeout)
                if still_pending:
                    raise TimeoutError(
                        "Adapters ignored cancellation; dependencies must remain open"
                    )
            await asyncio.gather(*tasks, return_exceptions=True)
        self._healthy()
