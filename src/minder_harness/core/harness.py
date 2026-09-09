from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from .cancellation import CancellationToken
from .context import TaskState
from .events import InMemoryEventCollector
from .loop import AgentLoop
from .models import (
    ExecutionLimits,
    JSONValue,
    Message,
    Run,
    Session,
    ToolDefinition,
    utc_now_iso,
)
from .ports import AttemptRecorder, EventEmitter, LLMProvider, ToolExecutor


class AgentHarness:
    """In-memory Run controller around the small AgentLoop."""

    def __init__(
        self,
        provider: LLMProvider,
        tool_executor: ToolExecutor,
        loop: AgentLoop | None = None,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor
        self.loop = AgentLoop() if loop is None else loop
        self._active_sessions: set[str] = set()

    async def run(
        self,
        session: Session,
        user_content: JSONValue,
        limits: ExecutionLimits | None = None,
        cancellation: CancellationToken | None = None,
        tools: Sequence[ToolDefinition] = (),
        *,
        existing_run: Run | None = None,
        event_sink: EventEmitter | None = None,
        recorder: AttemptRecorder | None = None,
        task_state: TaskState | None = None,
        context_limit: int = 24_000,
    ) -> Run:
        if session.session_id in self._active_sessions:
            raise ValueError("A run is already active for this session")
        self._active_sessions.add(session.session_id)
        try:
            return await self._run(
                session,
                user_content,
                limits,
                cancellation,
                tools,
                existing_run=existing_run,
                event_sink=event_sink,
                recorder=recorder,
                task_state=task_state,
                context_limit=context_limit,
            )
        finally:
            self._active_sessions.remove(session.session_id)

    async def _run(
        self,
        session: Session,
        user_content: JSONValue,
        limits: ExecutionLimits | None = None,
        cancellation: CancellationToken | None = None,
        tools: Sequence[ToolDefinition] = (),
        *,
        existing_run: Run | None = None,
        event_sink: EventEmitter | None = None,
        recorder: AttemptRecorder | None = None,
        task_state: TaskState | None = None,
        context_limit: int = 24_000,
    ) -> Run:
        selected_limits = (
            existing_run.limits
            if existing_run
            else (ExecutionLimits() if limits is None else limits)
        )
        token = CancellationToken() if cancellation is None else cancellation
        run = existing_run or Run(
            run_id=f"run_{uuid4().hex[:12]}",
            session_id=session.session_id,
            status="queued",
            limits=selected_limits,
        )
        collector = InMemoryEventCollector(session.session_id, run.run_id)
        emit = collector.emit if event_sink is None else event_sink
        run.events = collector.events
        user_message = Message(role="user", content=user_content)
        if existing_run is None:
            session.messages.append(user_message)
        session.updated_at = utc_now_iso()

        run.status = "running"
        run.started_at = utc_now_iso()
        await emit("run_started", {})
        result = await self.loop.run(
            tuple(session.messages),
            self.provider,
            self.tool_executor,
            selected_limits,
            token,
            emit,
            tools,
            run_id=run.run_id,
            recorder=recorder,
            task_state=task_state,
            context_limit=context_limit,
        )
        session.messages = list(result.messages)
        session.updated_at = utc_now_iso()
        run.usage = result.usage
        run.output = result.output
        run.error = result.error
        run.status = result.status
        run.finished_at = utc_now_iso()
        if result.status == "cancelled":
            run.cancel_requested_at = run.cancel_requested_at or utc_now_iso()
            await emit("run_cancelled", {})
        elif result.status == "completed":
            await emit("run_completed", {"output": result.output})
        elif result.status == "limit_exceeded":
            await emit(
                "run_limit_exceeded",
                {}
                if result.error is None
                else {
                    "error": result.error.details
                    | {"code": result.error.code, "message": result.error.message}
                },
            )
        else:
            await emit(
                "run_failed",
                {}
                if result.error is None
                else {
                    "error": result.error.details
                    | {"code": result.error.code, "message": result.error.message}
                },
            )
        return run
