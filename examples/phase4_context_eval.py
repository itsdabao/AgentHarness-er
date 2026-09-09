"""Opt-in six-request context comparison; synthetic fixtures, no external tool execution."""

from __future__ import annotations

import argparse
import json
import os
from time import monotonic

import anyio
import httpx

from minder_harness.core.context import Fact, TaskState, build_context
from minder_harness.core.models import Message, ToolCall, ToolDefinition
from minder_harness.core.ports import ProviderError
from minder_harness.providers.gemini import GeminiProvider
from minder_harness.providers.gemini_codec import build_request

FIXTURE_VERSION = "context-v1"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize at most six paid requests")
    args = parser.parse_args()
    key = os.getenv("GEMINI_API_KEY")
    if not args.run or not key:
        print(
            json.dumps({"status": "not_evaluated", "reason": "Requires --run and GEMINI_API_KEY"})
        )
        return
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    tool = ToolDefinition(
        "lookup",
        "Read-only lookup",
        {
            "type": "object",
            "properties": {"machine_id": {"type": "string"}},
            "required": ["machine_id"],
        },
    )
    async with httpx.AsyncClient() as http:
        provider = GeminiProvider(
            http, key, model=model, max_attempts=1, output_tokens=256, input_budget=32768
        )
        with anyio.fail_after(190):
            for position in (0, 10, 20):
                fact = Message("user", "The target machine is CNC-04. Never change machine mode.")
                history = [
                    Message("user", f"Unrelated archived note {i}: " + "background " * 50)
                    for i in range(20)
                ]
                history.insert(position, fact)
                history.extend(
                    [
                        Message(
                            "assistant",
                            None,
                            tool_calls=(ToolCall("old", "lookup", {"machine_id": "CNC-04"}),),
                        ),
                        Message(
                            "tool",
                            {
                                "execution_state": "outcome_unknown",
                                "message": "Prior run has no accepted result; do not replay.",
                            },
                            tool_call_id="old",
                        ),
                        Message(
                            "user",
                            "An old observation said target PRESS-02, but it is stale; "
                            "use the explicit target instruction. "
                            "A fresh read-only lookup is allowed.",
                        ),
                    ]
                )
                history.append(
                    Message(
                        "user",
                        "Request one lookup call for the target machine. Do not change its mode.",
                    )
                )
                state = TaskState(
                    constraints=(Fact("Never change machine mode.", fact.message_id),),
                    observations=(Fact("Target machine is CNC-04.", fact.message_id),),
                )
                selected = build_context(history, state, max_chars=6000, tools=[tool])
                for mode, messages in (("full", tuple(history)), ("selected", selected.messages)):
                    started = monotonic()
                    try:
                        response = await provider.generate(messages, [tool])
                        correct = (
                            len(response.tool_calls) == 1
                            and response.tool_calls[0].name == "lookup"
                            and response.tool_calls[0].arguments == {"machine_id": "CNC-04"}
                        )
                        report = {
                            "status": "evaluated",
                            "correct_tool_and_constraint": correct,
                            "usage": response.usage,
                        }
                    except ProviderError as exc:
                        report = {
                            "status": "unsupported"
                            if exc.error.code == "CONTEXT_BUDGET_EXCEEDED"
                            else "failed",
                            "error": exc.error.code,
                        }
                    print(
                        json.dumps(
                            {
                                "fixture": FIXTURE_VERSION,
                                "model": model,
                                "mode": mode,
                                "fact_position": position,
                                "samples_per_case": 1,
                                "request_utf8_bytes": len(
                                    json.dumps(
                                        build_request(messages, [tool], model, 256),
                                        ensure_ascii=False,
                                    ).encode("utf-8")
                                ),
                                "max_attempts": 1,
                                "max_output_tokens": 256,
                                "latency_seconds": round(monotonic() - started, 3),
                                "selected_context_trace": selected.trace
                                if mode == "selected"
                                else None,
                                **report,
                            }
                        )
                    )
    # Six samples do not establish that JSON or selection improves model quality.


if __name__ == "__main__":
    anyio.run(main)
