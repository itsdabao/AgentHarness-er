from __future__ import annotations

from collections.abc import Callable, Coroutine
from threading import Event as ThreadEvent
from typing import Any

import anyio


class CancellationToken:
    """Cooperative cancellation signal shared by the Harness and Agent Loop."""

    def __init__(self) -> None:
        self._event = ThreadEvent()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self, timeout_seconds: float) -> bool:
        """Wait until cancelled or the timeout elapses."""
        deadline = anyio.current_time() + timeout_seconds
        while not self.is_cancelled:
            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                return False
            await anyio.sleep(min(0.02, remaining))
        return True

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


class OperationCancelled(Exception):
    """The caller cancelled an asynchronous operation."""


async def cancellable[T](
    operation: Callable[[], Coroutine[Any, Any, T]],
    timeout_seconds: float | None,
    cancellation: CancellationToken | None = None,
    *,
    interruption: Callable[[], Exception | None] | None = None,
) -> T:
    """Bound local I/O without blocking the event loop or bypassing SDK cleanup shields."""
    deadline = float("inf") if timeout_seconds is None else anyio.current_time() + timeout_seconds
    abort: Exception | None = None
    failure: Exception | None = None

    async def watch(scope: anyio.CancelScope) -> None:
        nonlocal abort
        while True:
            if cancellation is not None and cancellation.is_cancelled:
                abort = OperationCancelled("The operation was cancelled.")
            if abort is None and interruption is not None:
                abort = interruption()
            remaining = deadline - anyio.current_time()
            if abort is None and remaining <= 0:
                abort = TimeoutError("The operation timed out.")
            if abort is not None:
                scope.cancel()
                return
            await anyio.sleep(min(0.02, remaining))

    if cancellation is not None and cancellation.is_cancelled:
        raise OperationCancelled("The operation was cancelled.")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise TimeoutError("The operation timed out.")
    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(watch, scope)
            try:
                result = await operation()
            except Exception as exc:
                failure = exc
            finally:
                tasks.cancel_scope.cancel()
    if abort is not None:
        raise abort
    if failure is not None:
        raise failure
    return result
