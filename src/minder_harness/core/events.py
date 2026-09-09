from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4

from .models import Event, JSONValue, utc_now_iso


class InMemoryEventCollector:
    """Collect ordered events for one Run while the current process is alive."""

    def __init__(self, session_id: str, run_id: str) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.events: list[Event] = []

    async def emit(
        self,
        event_type: str,
        payload: dict[str, JSONValue] | None = None,
    ) -> Event:
        event = Event(
            event_id=f"evt_{uuid4().hex[:12]}",
            session_id=self.session_id,
            run_id=self.run_id,
            sequence=len(self.events) + 1,
            type=event_type,
            timestamp=utc_now_iso(),
            payload={} if payload is None else payload,
        )
        self.events.append(event)
        return event

    def to_jsonable(self) -> list[dict[str, JSONValue]]:
        return [event.to_dict() for event in self.events]

    @staticmethod
    def sequences(events: Iterable[Event]) -> list[int]:
        return [event.sequence for event in events]
