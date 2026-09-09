from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from time import monotonic
from typing import Any

import anyio
import httpx
import pytest

from fakes import FakeToolExecutor
from minder_harness.core import (
    AgentHarness,
    CancellationToken,
    ExecutionLimits,
    Message,
    Session,
    ToolDefinition,
)
from minder_harness.core.codec import message_from
from minder_harness.core.models import as_jsonable
from minder_harness.core.ports import ModelExecutionContext, ProviderError, StorageError
from minder_harness.providers.gemini import GeminiProvider
from minder_harness.providers.gemini_codec import build_request, parse_response

pytestmark = pytest.mark.anyio
MODEL = "gemini-2.5-flash"


def answer(text: str = "done") -> dict[str, Any]:
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": text}],
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 21, "candidatesTokenCount": 3},
    }


def context(
    events: list[dict[str, Any]], token: CancellationToken | None = None, seconds: float = 5
) -> ModelExecutionContext:
    async def emit(kind: str, payload: Any = None) -> None:
        events.append({"type": kind, "payload": payload})

    return ModelExecutionContext(token or CancellationToken(), monotonic() + seconds, emit, 1)


@pytest.mark.parametrize(
    "status,code,attempts",
    [
        (400, "PROVIDER_INVALID_REQUEST", 1),
        (401, "PROVIDER_AUTHENTICATION", 1),
        (403, "PROVIDER_PERMISSION", 1),
        (404, "PROVIDER_MODEL_NOT_FOUND", 1),
        (429, "PROVIDER_RATE_LIMITED", 1),
        (500, "PROVIDER_UNAVAILABLE", 2),
        (503, "PROVIDER_UNAVAILABLE", 2),
    ],
)
async def test_http_classification_no_body_leak(status: int, code: str, attempts: int) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "TOP_SECRET"}})

    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = GeminiProvider(http, "TOP_SECRET", retry_delay=0)
        with pytest.raises(ProviderError) as caught:
            await provider.generate([Message("user", "hi")], context=context(events))
    assert caught.value.error.code == code
    assert len(calls) == attempts
    assert "TOP_SECRET" not in json.dumps(events)
    assert calls[0].headers["x-goog-api-key"] == "TOP_SECRET"
    assert "TOP_SECRET" not in str(calls[0].url)


async def test_retry_plan_precedes_wait_and_step_count_is_not_attempt_count() -> None:
    events: list[dict[str, Any]] = []
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"})
        assert any(e["payload"].get("retry_scheduled") for e in events)
        return httpx.Response(200, json=answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = GeminiProvider(http, "secret")
        result = await provider.generate([Message("user", "hi")], context=context(events))
        assert result.usage == {"input_tokens": 21, "output_tokens": 3}
        calls = 0
        run = await AgentHarness(provider, FakeToolExecutor()).run(Session("s"), "hi")
    assert run.status == "completed"
    assert run.usage.model_steps == 1 and calls == 2
    assert run.usage.input_tokens == 21


@pytest.mark.parametrize("hint", ["100", "invalid"])
async def test_429_unusable_hint_never_retries(hint: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": hint})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ProviderError, match="quota"):
            await GeminiProvider(http, "secret").generate(
                [Message("user", "hi")], context=context([], seconds=0.5)
            )
    assert calls == 1


@pytest.mark.parametrize("in_retry", [False, True])
async def test_cancel_during_http_or_retry_prevents_further_requests(in_retry: bool) -> None:
    entered = anyio.Event()
    token = CancellationToken()
    events: list[dict[str, Any]] = []
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if in_retry:
            return httpx.Response(429, headers={"Retry-After": "2"})
        entered.set()
        await anyio.sleep(30)
        return httpx.Response(200, json=answer("late"))

    ctx = context(events, token)
    original = ctx.emit

    async def emit(kind: str, payload: Any = None) -> None:
        await original(kind, payload)
        if payload and payload.get("retry_scheduled"):
            entered.set()

    ctx = replace(ctx, emit=emit)
    from minder_harness.core.cancellation import OperationCancelled

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with anyio.fail_after(3):
            task = asyncio.create_task(
                GeminiProvider(http, "secret").generate([Message("user", "hi")], context=ctx)
            )
            await entered.wait()
            token.cancel()
            with pytest.raises(OperationCancelled):
                await task
    assert calls == 1


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, ValueError])
async def test_transport_retry_and_unknown_error_policy(error_type: type[Exception]) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error_type("sensitive transport details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ProviderError) as caught:
            await GeminiProvider(http, "secret", retry_delay=0).generate([Message("user", "hi")])
    assert calls == (1 if error_type is ValueError else 2)
    assert "sensitive" not in str(caught.value)


async def test_malformed_response_never_retries_and_context_budget_prevents_dispatch() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"candidates": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        provider = GeminiProvider(http, "secret")
        with pytest.raises(ProviderError) as caught:
            await provider.generate([Message("user", "hi")])
        assert caught.value.error.code == "MALFORMED_MODEL_RESPONSE"
        tool = ToolDefinition("large", "x" * 40000, {"type": "object"})
        with pytest.raises(ProviderError) as caught:
            await provider.generate([Message("user", "hi")], [tool])
        assert caught.value.error.code == "CONTEXT_BUDGET_EXCEEDED"
    assert calls == 1


async def test_provider_event_storage_failure_stops_before_http() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=answer())

    async def fail_record(kind: str, payload: Any = None) -> None:
        if payload and payload.get("record_type") == "provider_attempt":
            raise StorageError("disk")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(StorageError):
            await AgentHarness(GeminiProvider(http, "secret"), FakeToolExecutor()).run(
                Session("s"),
                "hi",
                event_sink=fail_record,
            )
    assert calls == 0


async def test_deadline_interrupts_real_adapter_wait() -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await anyio.sleep(30)
        return httpx.Response(200, json=answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with anyio.fail_after(2):
            run = await AgentHarness(GeminiProvider(http, "secret"), FakeToolExecutor()).run(
                Session("s"), "hi", limits=ExecutionLimits(timeout_seconds=0.05)
            )
    assert run.status == "limit_exceeded" and calls == 1


def test_tool_translation_roundtrip_preserves_signature_and_omits_thoughts() -> None:
    wire = answer()
    wire["candidates"][0]["content"]["parts"] = [
        {"text": "PRIVATE REASONING", "thought": True},
        {"text": "Checking two machines."},
        {
            "functionCall": {"id": "a", "name": "status", "args": {"id": "A"}},
            "thoughtSignature": "opaque-signature",
        },
        {"functionCall": {"id": "b", "name": "status", "args": {"id": "B"}}},
    ]
    response = parse_response(wire, MODEL)
    assert response.content is None and len(response.tool_calls) == 2
    message = Message(
        "assistant", None, tool_calls=response.tool_calls, provider_data=response.provider_data
    )
    saved = message_from(json.loads(json.dumps(as_jsonable(message))))
    assert "PRIVATE" not in json.dumps(as_jsonable(saved))
    request = build_request(
        [
            Message("user", "check"),
            saved,
            Message("tool", {"status": "ok"}, tool_call_id="a"),
            Message("tool", {"execution_state": "outcome_unknown"}, tool_call_id="b"),
            Message("user", "what next"),
        ],
        [],
        MODEL,
        1024,
    )
    assert request["contents"][1]["parts"][1]["thoughtSignature"] == "opaque-signature"
    results = request["contents"][2]["parts"]
    assert [p["functionResponse"]["id"] for p in results] == ["a", "b"]
    assert (
        results[1]["functionResponse"]["response"]["result"]["execution_state"] == "outcome_unknown"
    )
    with pytest.raises(ProviderError):
        build_request([saved, Message("user", "incomplete")], [], MODEL, 1024)


def test_truncated_and_duplicate_calls_are_rejected() -> None:
    wire = answer()
    wire["candidates"][0]["finishReason"] = "MAX_TOKENS"
    with pytest.raises(ProviderError):
        parse_response(wire, MODEL)
    wire["candidates"][0]["finishReason"] = "STOP"
    wire["candidates"][0]["content"]["parts"] = [
        {"functionCall": {"id": "same", "name": "status", "args": {}}}
    ] * 2
    with pytest.raises(ProviderError):
        parse_response(wire, MODEL)
