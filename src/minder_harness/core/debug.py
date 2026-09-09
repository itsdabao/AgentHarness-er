from __future__ import annotations

import json
from collections.abc import Sequence

from .models import Event


def render_event_trace(events: Sequence[Event]) -> str:
    """Return a readable trace without printing or mutating execution state."""
    if not events:
        return "No events."

    lines = [f"RUN {events[0].run_id}"]
    for event in events:
        lines.append(f"{event.sequence:03d} {event.type}")
        if event.type == "tool_call_requested":
            lines.append(
                "MODEL -> "
                f"tool_call {event.payload.get('tool_name')} "
                f"{json.dumps(event.payload.get('arguments', {}), sort_keys=True)}"
            )
        elif event.type == "tool_execution_completed":
            lines.append(
                f"TOOL -> {json.dumps(event.payload.get('model_content'), sort_keys=True)}"
            )
        elif event.type == "tool_execution_failed":
            lines.append(f"TOOL ERROR -> {json.dumps(event.payload.get('error'), sort_keys=True)}")
        elif event.type == "run_completed":
            lines.append(f"MODEL -> {json.dumps(event.payload.get('output'), sort_keys=True)}")
    return "\n".join(lines)
