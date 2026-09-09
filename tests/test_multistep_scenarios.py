"""Scripted decisions, real loop/SQLite/MCP. These are not Gemini reasoning evals."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import anyio
import pytest
from mcp import StdioServerParameters

from fakes import FakeLLMProvider
from fixtures.factory_data import dataset
from minder_harness.core import ExecutionLimits, Message, ModelResponse, Run, ToolCall
from minder_harness.core.models import JSONValue
from minder_harness.persistence import SQLiteExecutionStore
from support import ProviderGate, open_runtime, rows

pytestmark = pytest.mark.anyio
Script = Generator[ToolCall | tuple[ToolCall, ...], Any, dict[str, Any]]
PRIORITY = {"low": 1, "normal": 2, "high": 3, "critical": 4}
TASKS = {
    "H01": (
        "Kiểm tra CNC-04, tìm công việc ưu tiên cao nhất của máy trong khu vực tương ứng, "
        "rồi lấy quy trình của công việc đó."
    ),
    "H02": (
        "Trong khu {area}, chọn công việc ưu tiên cao nhất, kiểm tra máy liên quan "
        "và lấy quy trình nếu máy đang cảnh báo."
    ),
    "H03": (
        "Kiểm tra các máy có công việc ở {area}. Chọn máy cảnh báo có nhiệt độ cao nhất "
        "rồi lấy quy trình của công việc liên quan."
    ),
    "H04": (
        "Kiểm tra CNC-04 và PRESS-02; với mỗi máy tìm công việc ưu tiên cao nhất và quy trình. "
        "Báo cáo riêng từng máy."
    ),
    "H05": (
        "Kiểm tra CNC-04, lấy công việc và quy trình. Đọc lại trạng thái một lần trước khi "
        "kết luận, ghi rõ trước và sau nếu thay đổi."
    ),
}


def call(name: str, **arguments: JSONValue) -> ToolCall:
    return ToolCall(uuid4().hex, name, arguments)


def scripted_provider(script: Script, gate: ProviderGate | None = None) -> FakeLLMProvider:
    """Drive a generator with actual tool messages, never fixture answers."""
    pending: tuple[ToolCall, ...] = ()

    def respond(messages: Sequence[Message]) -> ModelResponse:
        nonlocal pending
        replies = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
        try:
            if not pending:
                requested = next(script)
            else:
                # A missing or mis-correlated result fails the fake, not silently advances it.
                results = [replies[item.tool_call_id] for item in pending]
                requested = script.send(results[0] if len(pending) == 1 else results)
        except StopIteration as finished:
            return ModelResponse(content=cast(JSONValue, finished.value))
        pending = requested if isinstance(requested, tuple) else (requested,)
        return ModelResponse(tool_calls=pending)

    return FakeLLMProvider(respond=respond, on_generate=gate)


def work_orders(value: Any) -> list[dict[str, Any]]:
    # MCP wraps structured non-object outputs in a result property.
    return cast(list[dict[str, Any]], value["result"] if isinstance(value, dict) else value)


def highest(orders: list[dict[str, Any]], machine_id: str | None = None) -> dict[str, Any]:
    matches = [o for o in orders if machine_id is None or o["machine_id"] == machine_id]
    return max(matches, key=lambda order: PRIORITY[order["priority"]])


def machine_chain(machine_id: str) -> Script:
    machine = yield call("get_machine_status", machine_id=machine_id)
    orders = work_orders((yield call("list_open_work_orders", area=machine["area"])))
    order = highest(orders, machine["machine_id"])
    if "procedure_code" not in order:
        return {"machine": machine, "missing": "procedure_code"}
    procedure = yield call("get_safety_procedure", procedure_code=order["procedure_code"])
    return {"machine": machine, "order": order, "procedure": procedure}


def choose_then_check(area: str) -> Script:
    order = highest(work_orders((yield call("list_open_work_orders", area=area))))
    machine = yield call("get_machine_status", machine_id=order["machine_id"])
    procedure = None
    if machine["status"] == "warning":
        procedure = yield call("get_safety_procedure", procedure_code=order["procedure_code"])
    return {"machine": machine, "order": order, "procedure": procedure}


def compare_machines(area: str, batch: bool = False) -> Script:
    orders = work_orders((yield call("list_open_work_orders", area=area)))
    requests = tuple(
        call("get_machine_status", machine_id=key)
        for key in dict.fromkeys(o["machine_id"] for o in orders)
    )
    readings = []
    if batch:
        readings = yield requests
    else:
        for request in requests:
            readings.append((yield request))
    missing = [r for r in readings if "error_code" in r]
    if missing:
        return {"complete": False, "readings": readings, "errors": missing}
    winner = max(
        (r for r in readings if r["status"] == "warning"),
        key=lambda reading: reading["temperature_c"],
    )
    order = highest(orders, winner["machine_id"])
    procedure = yield call("get_safety_procedure", procedure_code=order["procedure_code"])
    return {"machine": winner, "order": order, "procedure": procedure}


def two_chains() -> Script:
    first = yield from machine_chain("CNC-04")
    second = yield from machine_chain("PRESS-02")
    return {"machines": [first, second]}


def recheck() -> Script:
    report = yield from machine_chain("CNC-04")
    latest = yield call("get_machine_status", machine_id=report["machine"]["machine_id"])
    return report | {"latest": latest}


def untrusted_summary(area: str) -> Script:
    orders = work_orders((yield call("list_open_work_orders", area=area)))
    if "delete_factory" not in orders[0]["summary"]:
        raise ValueError("Injection fixture did not reach the model")
    # Deliberately simulate a model following the injection; test the executor boundary.
    rejection = yield call("delete_factory")
    return {"rejection": rejection}


def target_for(tmp_path: Path, variant: str, mode: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "fixtures.multistep_mcp",
            "--variant",
            variant,
            "--mode",
            mode,
            "--log",
            str(tmp_path / "dispatch.jsonl"),
        ],
        cwd=str(Path(__file__).parent),
    )


@dataclass
class Evidence:
    run: Run
    provider: FakeLLMProvider
    dispatches: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    events: list[dict[str, Any]]
    messages: list[Message]


async def execute(
    tmp_path: Path,
    script: Script,
    *,
    variant: str = "alpha",
    mode: str = "normal",
    limits: ExecutionLimits | None = None,
    gate: ProviderGate | None = None,
    task: str = TASKS["H01"],
) -> Evidence:
    provider = scripted_provider(script, gate)
    path = tmp_path / "scenario.db"
    with anyio.fail_after(20):
        async with open_runtime(
            path,
            provider,
            mcp_target=target_for(tmp_path, variant, mode),
            call_timeout=0.2 if mode == "timeout" else 2,
        ) as runtime:
            session = await runtime.service.create_session()
            run = await runtime.service.submit_task(session.session_id, task, limits=limits)
            if gate is not None:
                await gate.entered.wait()
                await runtime.service.cancel_run(run.run_id)
            finished = await runtime.service.wait_run(run.run_id)
        # Read durable evidence after closing and reopening, not just in-memory objects.
        async with SQLiteExecutionStore(path) as store:
            persisted = await store.get_run(run.run_id)
            messages = (await store.get_session(session.session_id)).messages
    if persisted != finished:
        raise ValueError("Persisted run does not match the completed service snapshot")
    attempts = [
        {"call": json.loads(c), "result": json.loads(r), "state": s}
        for c, r, s in rows(path, "SELECT call_json,result_json,state FROM attempts ORDER BY rowid")
    ]
    return Evidence(
        persisted,
        provider,
        [
            json.loads(line)
            for line in (tmp_path / "dispatch.jsonl").read_text(encoding="utf-8").splitlines()
        ],
        attempts,
        [json.loads(row[0]) for row in rows(path, "SELECT body FROM events ORDER BY sequence")],
        messages,
    )


@pytest.mark.parametrize("variant", ["alpha", "beta"])
@pytest.mark.parametrize(
    "case, count", [("H01", 3), ("H02", 3), ("H03", 5), ("H04", 6), ("H05", 4)]
)
async def test_happy_dependency_chains(tmp_path: Path, variant: str, case: str, count: int) -> None:
    data = dataset(variant)
    scripts = {
        "H01": machine_chain("CNC-04"),
        "H02": choose_then_check(data["area"]),
        "H03": compare_machines(data["area"]),
        "H04": two_chains(),
        "H05": recheck(),
    }
    evidence = await execute(
        tmp_path,
        scripts[case],
        variant=variant,
        mode="changing" if case == "H05" else "normal",
        task=TASKS[case].format(area=data["area"]),
    )
    run = evidence.run
    assert run.status == "completed", run.error
    assert run.usage.tool_calls == count
    assert run.usage.model_steps == count + 1 == len(evidence.provider.calls)
    assert len(evidence.dispatches) == len(evidence.attempts) == count
    assert all(item["pid"] != os.getpid() for item in evidence.dispatches)
    assert [e["sequence"] for e in evidence.events] == list(range(1, len(evidence.events) + 1))
    assert evidence.events[-1]["type"] == "run_completed"
    assert evidence.messages[-1].content == run.output
    assert any(
        m.role == "user" and m.content == TASKS[case].format(area=data["area"])
        for m in evidence.provider.calls[0]
    )
    previous_result_sequence = 0
    for attempt, dispatch in zip(evidence.attempts, evidence.dispatches, strict=True):
        request, result = attempt["call"], attempt["result"]
        assert attempt["state"] == "succeeded"
        assert dispatch["tool"] == request["name"]
        assert dispatch["key"] in request["arguments"].values()
        intent = next(
            e
            for e in evidence.events
            if e["payload"].get("record_type") == "attempt_intent"
            and e["payload"]["execution_id"] == request["execution_id"]
        )
        result_event = next(
            e
            for e in evidence.events
            if e["payload"].get("tool_call_id") == request["tool_call_id"]
            and "_message" in e["payload"]
            and e["type"] == "tool_execution_completed"
        )
        assert previous_result_sequence < intent["sequence"] < result_event["sequence"]
        previous_result_sequence = result_event["sequence"]
        # Every returned value really appeared in a later provider input with the right call ID.
        assert any(
            message.tool_call_id == request["tool_call_id"]
            and message.content == result["model_content"]
            for turn in evidence.provider.calls
            for message in turn
            if message.role == "tool"
        )
    report = run.output
    assert isinstance(report, dict)
    reports = report["machines"] if case == "H04" else [report]
    assert isinstance(reports, list)
    machine_ids = []
    for item in reports:
        assert isinstance(item, dict)
        machine, order, procedure = item["machine"], item["order"], item["procedure"]
        assert isinstance(machine, dict) and isinstance(order, dict) and isinstance(procedure, dict)
        machine_ids.append(machine["machine_id"])
        assert order["machine_id"] == machine["machine_id"]
        assert procedure == data["procedures"][order["procedure_code"]]
        assert order == highest(data["orders"][machine["area"]], str(machine["machine_id"]))
    if case in {"H02", "H03"}:
        assert report["machine"] == data["machines"][f"M-{variant}-2"]
    if case == "H04":
        assert machine_ids == ["CNC-04", "PRESS-02"]
    if case == "H05":
        first, latest = report["machine"], report["latest"]
        assert isinstance(first, dict) and isinstance(latest, dict)
        assert first["version"] == 1 and latest["version"] == 2
        assert first["status"] == "warning" and latest["status"] == "running"
        assert str(latest["observed_at"]) > str(first["observed_at"])


async def test_independent_reads_can_share_one_model_step(tmp_path: Path) -> None:
    evidence = await execute(tmp_path, compare_machines(dataset("alpha")["area"], batch=True))
    assert evidence.run.status == "completed", evidence.run.error
    assert evidence.run.usage.model_steps == 4 and evidence.run.usage.tool_calls == 5
    assert len(evidence.dispatches) == 5
    assert len(evidence.provider.calls[2]) > len(evidence.provider.calls[1])
    assert len([m for m in evidence.provider.calls[2] if m.role == "tool"]) == 4


async def test_E01_insufficient_step_budget(tmp_path: Path) -> None:
    evidence = await execute(tmp_path, machine_chain("CNC-04"), limits=ExecutionLimits(max_steps=3))
    assert evidence.run.status == "limit_exceeded" and evidence.run.output is None
    assert evidence.run.usage.model_steps == 3
    assert len(evidence.dispatches) == len(evidence.attempts) == 2
    assert [d["tool"] for d in evidence.dispatches] == [
        "get_machine_status",
        "list_open_work_orders",
    ]


async def test_E02_missing_procedure_link(tmp_path: Path) -> None:
    evidence = await execute(tmp_path, machine_chain("CNC-04"), mode="missing")
    assert evidence.run.status == "completed"
    assert (
        isinstance(evidence.run.output, dict) and evidence.run.output["missing"] == "procedure_code"
    )
    assert len(evidence.dispatches) == len(evidence.attempts) == 2
    assert all(d["tool"] != "get_safety_procedure" for d in evidence.dispatches)


async def test_E03_partial_comparison_after_timeout(tmp_path: Path) -> None:
    evidence = await execute(tmp_path, compare_machines(dataset("alpha")["area"]), mode="timeout")
    assert evidence.run.status == "completed", evidence.run.error
    report = evidence.run.output
    assert isinstance(report, dict) and report["complete"] is False
    assert "machine" not in report  # No claimed winner with incomplete readings.
    assert len(evidence.dispatches) == len(evidence.attempts) == 4
    assert [a["state"] for a in evidence.attempts].count("succeeded") == 3
    failed = [a for a in evidence.attempts if a["state"] == "failed"]
    assert len(failed) == 1 and failed[0]["result"]["error"]["code"] == "MCP_TIMEOUT"
    assert any(e["type"] == "tool_execution_failed" for e in evidence.events)


async def test_E04_cancel_before_third_dispatch(tmp_path: Path) -> None:
    gate = ProviderGate(at_step=3)
    evidence = await execute(tmp_path, machine_chain("CNC-04"), gate=gate)
    assert evidence.run.status == "cancelled" and evidence.run.output is None
    assert gate.entered.is_set() and gate.cancelled.is_set()
    assert len(evidence.dispatches) == len(evidence.attempts) == 2
    assert all(a["state"] == "succeeded" for a in evidence.attempts)
    assert len([m for m in evidence.provider.calls[-1] if m.role == "tool"]) == 2
    assert evidence.events[-1]["type"] == "run_cancelled"


async def test_E05_unapproved_tool_from_injected_data_is_blocked(tmp_path: Path) -> None:
    evidence = await execute(
        tmp_path, untrusted_summary(dataset("alpha")["area"]), mode="injection"
    )
    assert evidence.run.status == "completed", evidence.run.error
    assert evidence.run.usage.tool_calls == 2  # One request rejected locally; one dispatched.
    assert [d["tool"] for d in evidence.dispatches] == ["list_open_work_orders"]
    assert len(evidence.attempts) == 1
    assert isinstance(evidence.run.output, dict)
    rejection = evidence.run.output["rejection"]
    assert isinstance(rejection, dict) and rejection["error_code"] == "TOOL_NOT_APPROVED"
    assert any(
        e["type"] == "tool_execution_failed" and e["payload"]["tool_name"] == "delete_factory"
        for e in evidence.events
    )
