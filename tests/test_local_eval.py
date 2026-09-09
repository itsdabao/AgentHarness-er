"""Offline codec regressions for the opt-in local evaluation adapter."""

import json
from typing import Any

import pytest

from eval_local_models import grade
from local_provider import decode_response, encode_messages
from minder_harness.core import Message, ToolCall


def response(message: dict[str, Any], finish_reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def test_local_tool_exchange_keeps_ids_and_json() -> None:
    call = ToolCall("call-1", "read", {"machine_id": "CNC-04"})
    messages = encode_messages(
        [
            Message("user", "check"),
            Message("assistant", None, tool_calls=(call,)),
            Message("tool", {"status": "warning"}, tool_call_id="call-1"),
        ]
    )
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == call.arguments
    assert messages[2]["tool_call_id"] == "call-1"
    assert json.loads(messages[2]["content"]) == {"status": "warning"}


def test_local_tool_decision_does_not_become_premature_final() -> None:
    decoded = decode_response(
        response(
            {
                "content": "I will check",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            }
        )
    )
    assert decoded.content is None
    assert decoded.tool_calls == (ToolCall("call-1", "read", {}),)
    assert decoded.validation_error() is None


def test_local_final_text_and_usage() -> None:
    data = response({"content": '{"found":false}'})
    data["usage"] = {"prompt_tokens": 12, "completion_tokens": 5}
    decoded = decode_response(data)
    assert decoded.content == '{"found":false}'
    assert decoded.usage == {"input_tokens": 12, "output_tokens": 5}


@pytest.mark.parametrize("arguments", ["[]", "not-json"])
def test_local_bad_arguments_are_rejected(arguments: str) -> None:
    with pytest.raises(ValueError):
        decode_response(
            response(
                {
                    "tool_calls": [
                        {"id": "x", "function": {"name": "read", "arguments": arguments}}
                    ],
                }
            )
        )


def test_local_truncated_final_is_rejected() -> None:
    with pytest.raises(ValueError, match="truncated"):
        decode_response(response({"content": "partial"}, "length"))


def test_local_empty_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="No final"):
        decode_response(response({"content": ""}))


def grading_evidence(output: Any) -> dict[str, Any]:
    return {
        "run": {"status": "completed", "output": json.dumps(output)},
        "attempts": [{"state": "succeeded"}],
        "dispatches": [{"tool": "get_machine_status", "key": "CNC-04"}],
        "events": [{"sequence": 1, "payload": {}}],
        "persisted_equal": True,
    }


def test_grade_single_requires_real_read_and_correct_values() -> None:
    evidence = grading_evidence(
        {
            "machine_id": "CNC-04",
            "status": "warning",
            "temperature_c": 78.4,
        }
    )
    assert grade("D_single", evidence)["passed"]
    evidence["dispatches"] = []
    assert not grade("D_single", evidence)["passed"]


def test_grade_rejects_wrong_final_shape() -> None:
    evidence = grading_evidence([{"machine_id": "CNC-04"}])
    assert not grade("H04", evidence)["checks"]["json_object"]


def test_grade_requires_comparison_observations_not_lucky_answer() -> None:
    evidence = grading_evidence({})
    assert not grade("H03", evidence)["checks"]["all_candidates_before_decision"]


def test_grade_distinguishes_tool_attempt_from_fixture_dictionary_read() -> None:
    evidence = grading_evidence({})
    evidence["attempts"].append({"state": "failed"})
    assert grade("H05", evidence)["checks"]["server_read_count_consistent"]


def test_grade_cancel_requires_inflight_request() -> None:
    evidence = grading_evidence(None)
    evidence.update(
        run={"status": "cancelled", "output": None},
        attempts=[],
        dispatches=[],
        cancel_inflight=False,
        cancel_seconds=0.01,
    )
    assert not grade("C_cancel", evidence)["passed"]
